import collections
from enum import IntEnum, unique
from multiprocessing import Lock
from typing import Any, Callable, Optional, TYPE_CHECKING

import unicorn

import cemu.const
import cemu.core
import cemu.os
import cemu.utils
from cemu.const import (
    MEMORY_TEXT_SECTION_NAME,
    MEMORY_DATA_SECTION_NAME,
    MEMORY_STACK_SECTION_NAME,
)
from cemu.exceptions import CemuEmulatorMissingRequiredSection
from cemu.log import dbg, error, info, warn
from .arch import is_x86, is_x86_32, x86
from .memory import MemorySection
from .ui.utils import popup, PopupType


if TYPE_CHECKING:
    import cemu.arch


@unique
class EmulatorState(IntEnum):
    INVALID = 0
    """An invalid state, ideally should never be here"""
    STARTING = 1
    """CEmu is starting"""
    NOT_RUNNING = 2
    """CEmu is started, but no emulation context is initialized"""
    IDLE = 3
    """The VM is running but stopped: used for stepping mode"""
    RUNNING = 4
    """The VM is running"""
    TEARDOWN = 5
    """Emulation is finishing"""
    FINISHED = 6
    """The VM has reached the end of the execution"""


MEMORY_MAP_DEFAULT_LAYOUT: list[MemorySection] = [
    MemorySection(MEMORY_TEXT_SECTION_NAME, 0x00004000, 0x1000, "READ|EXEC"),
    MemorySection(MEMORY_DATA_SECTION_NAME, 0x00005000, 0x1000, "READ|WRITE"),
    MemorySection(MEMORY_STACK_SECTION_NAME, 0x00006000, 0x4000, "READ|WRITE"),
]


class EmulationRegisters(collections.UserDict):
    data: dict[str, int]

    def __getitem__(self, key: str) -> int:
        """Thin wrapper around `dict` `__getitem__` for register: try to refresh the value to its latest value from the
        emulator.

        Args:
            key (str): register name

        Returns:
            int: the register value
        """
        assert cemu.core.context
        emu = cemu.core.context.emulator
        if emu.state in (EmulatorState.RUNNING, EmulatorState.IDLE, EmulatorState.FINISHED) and key in self.data.keys():
            val = emu.get_register_value(key)
            if val is not None:
                super().__setitem__(key, val)
        return super().__getitem__(key)


class Emulator:
    def __init__(self):
        self.use_step_mode = False
        self.widget = None
        self.lock = Lock()
        self.state: EmulatorState = EmulatorState.STARTING
        self.__state_change_callbacks: dict[EmulatorState, list[Callable]] = {
            EmulatorState.STARTING: [],
            EmulatorState.NOT_RUNNING: [],
            EmulatorState.IDLE: [],
            EmulatorState.RUNNING: [],
            EmulatorState.TEARDOWN: [],
            EmulatorState.FINISHED: [],
        }
        self.threaded_runner: Optional[object] = None
        self.vm: Optional[unicorn.Uc] = None
        self.code: bytes = b""
        self.codelines: str = ""
        self.sections: list[MemorySection] = []
        self.registers: EmulationRegisters = EmulationRegisters({})
        self.start_addr: int = 0

        #
        # A call to `reset` **MUST** be done once the program is fully loaded
        #
        return

    def reset(self):
        self.vm = None
        self.code = b""
        self.sections = MEMORY_MAP_DEFAULT_LAYOUT[:]
        assert cemu.core.context
        self.registers = EmulationRegisters({name: 0 for name in cemu.core.context.architecture.registers})
        self.start_addr = 0
        self.set(EmulatorState.NOT_RUNNING)
        return

    def __str__(self) -> str:
        if self.is_running:
            return f"Emulator is running, IP={self.pc()}, SP={self.sp()}"
        return "Emulator instance is not running"

    def get_register_value(self, regname: str) -> Optional[int]:
        """
        Returns an integer value of the register passed as a string.
        """

        if not self.vm:
            return None

        assert cemu.core.context
        arch = cemu.core.context.architecture
        ur = arch.uc_register(regname)
        val = self.vm.reg_read(ur)

        # TODO handle extended regs later
        assert isinstance(val, int)
        return val

    regs = get_register_value

    def pc(self) -> int:
        """
        Returns the current value of $pc
        """
        assert cemu.core.context
        # return self.get_register_value(cemu.core.context.architecture.pc)
        assert cemu.core.context
        return self.registers[cemu.core.context.architecture.pc]

    def sp(self) -> int:
        """
        Returns the current value of $sp
        """
        assert cemu.core.context
        # return self.get_register_value(cemu.core.context.architecture.sp)
        assert cemu.core.context
        return self.registers[cemu.core.context.architecture.sp]

    def setup(self) -> None:
        """
        Create a new VM, and sets up the hooks
        """
        if self.vm:
            #
            # Environment already setup, just resume
            #
            return

        info("Setting up emulation environment...")

        assert cemu.core.context
        arch = cemu.core.context.architecture
        self.vm = arch.uc
        self.vm.hook_add(unicorn.UC_HOOK_BLOCK, self.hook_block)
        self.vm.hook_add(unicorn.UC_HOOK_CODE, self.hook_code)
        self.vm.hook_add(unicorn.UC_HOOK_INTR, self.hook_interrupt)  # type: ignore
        self.vm.hook_add(unicorn.UC_HOOK_MEM_WRITE, self.hook_mem_access)
        self.vm.hook_add(unicorn.UC_HOOK_MEM_READ, self.hook_mem_access)
        assert cemu.core.context
        if is_x86(cemu.core.context.architecture):
            self.vm.hook_add(
                unicorn.UC_HOOK_INSN,
                self.hook_syscall,
                None,
                1,
                0,
                unicorn.x86_const.UC_X86_INS_SYSCALL,
            )

        if not self.__populate_memory():
            raise RuntimeError("populate_memory() failed")

        if not self.__populate_vm_registers():
            raise RuntimeError("populate_registers() failed")

        if not self.__populate_text_section():
            raise Exception("populate_text_section() failed")

        return

    def __populate_memory(self) -> bool:
        """
        Uses the information from `sections` to populate the unicorn VM memory layout
        """
        if not self.vm:
            error("VM is not initalized")
            return False

        if len(self.sections) < 1:
            error("No section declared")
            return False

        for section in self.sections:
            self.vm.mem_map(section.address, section.size, perms=section.permission.unicorn())
            msg = f"Mapping {str(section)}"

            if section.content:
                self.vm.mem_write(section.address, section.content)
                msg += f", imported data '{len(section.content)}'"

            dbg(f"[vm::setup] {msg}")

        #
        # Set temporary values to start_addr and end_addr.
        # Those values will likely be changed when populating text section
        #
        self.start_addr = self.sections[0].address
        self.end_addr = -1
        return True

    def __populate_vm_registers(self) -> bool:
        """
        Populates the VM memory layout according to the values given as parameter.
        """
        if not self.vm:
            return False

        assert cemu.core.context
        arch = cemu.core.context.architecture

        #
        # Set the initial IP if unspecified
        #
        if self.registers[arch.pc] == 0:
            section = self.find_section(cemu.const.MEMORY_TEXT_SECTION_NAME)
            self.registers[arch.pc] = section.address
            warn(f"No value specified for PC register, setting to {self.registers[arch.pc]:#x}")

        #
        # Set the initial SP if unspecified, in the middle of the stack section
        #
        if self.registers[arch.sp] == 0:
            section = self.find_section(MEMORY_STACK_SECTION_NAME)
            self.registers[arch.sp] = section.address + (section.size // 2)
            warn(f"No value specified for SP register, setting to {self.registers[arch.sp]:#x}")

        #
        # Populate all the registers for unicorn
        #
        if is_x86_32(arch):
            # create fake selectors
            ## required
            text = self.find_section(MEMORY_TEXT_SECTION_NAME)
            self.registers["CS"] = int(
                x86.X86_32.SegmentDescriptor(
                    text.address >> 8,
                    x86.X86_32.SegmentType.Code | x86.X86_32.SegmentType.Accessed,
                    False,
                    3,
                    True,
                )
            )

            data = self.find_section(MEMORY_DATA_SECTION_NAME)
            self.registers["DS"] = int(
                x86.X86_32.SegmentDescriptor(
                    data.address >> 8,
                    x86.X86_32.SegmentType.Data | x86.X86_32.SegmentType.Accessed,
                    False,
                    3,
                    True,
                )
            )

            stack = self.find_section(MEMORY_STACK_SECTION_NAME)
            self.registers["SS"] = int(
                x86.X86_32.SegmentDescriptor(
                    stack.address >> 8,
                    x86.X86_32.SegmentType.Data | x86.X86_32.SegmentType.Accessed | x86.X86_32.SegmentType.ExpandDown,
                    False,
                    3,
                    True,
                )
            )
            ## optional
            self.registers["GS"] = 0
            self.registers["FS"] = 0
            self.registers["ES"] = 0

        for regname, regvalue in self.registers.items():
            if regname in x86.X86_32.selector_registers:
                continue

            self.vm.reg_write(arch.uc_register(regname), regvalue)

        dbg(f"[vm::setup] Registers {self.registers}")
        return True

    def __refresh_registers_from_vm(self) -> bool:
        """Refresh the emulation register hashmap by reading values from the VM

        Returns:
            bool: True on success, False otherwise
        """
        if not self.vm or not self.is_running:
            return False

        assert cemu.core.context
        arch = cemu.core.context.architecture
        for regname in self.registers.keys():
            value = self.vm.reg_read(arch.uc_register(regname))
            assert isinstance(value, int)
            self.registers[regname] = value

        return True

    def __generate_text_bytecode(self) -> bool:
        """Compile the assembly code using Keystone.

        Returns:
            bool True if all went well, False otherwise.
        """
        assert cemu.core.context
        dbg(f"[vm::setup] Generating assembly code for {cemu.core.context.architecture.name}")

        try:
            insns = cemu.arch.assemble(self.codelines, base_address=self.start_addr)
            if len(insns) == 0:
                raise Exception("no instruction")
        except Exception as e:
            error(f"Failed to compile: exception {e.__class__.__name__}: {str(e)}")
            return False

        self.code = b"".join([insn.bytes for insn in insns])
        dbg(f"[vm::setup] {len(insns)} instruction(s) compiled: {len(self.code)} bytes")

        self.end_addr = self.start_addr + len(self.code)
        return True

    def validate_assembly_code(self) -> bool:
        return self.__generate_text_bytecode()

    def __populate_text_section(self) -> bool:
        if not self.vm:
            return False

        for secname in (
            MEMORY_TEXT_SECTION_NAME,
            MEMORY_DATA_SECTION_NAME,
            MEMORY_STACK_SECTION_NAME,
        ):
            try:
                self.find_section(secname)
            except KeyError:
                raise CemuEmulatorMissingRequiredSection(secname)

        text_section = self.find_section(MEMORY_TEXT_SECTION_NAME)
        info(f"Using text section {text_section}")

        if not self.__generate_text_bytecode():
            error("__generate_text_bytecode() failed")
            return False

        assert isinstance(self.code, bytes)

        dbg(f"Populated text section {text_section} with {len(self.code)} compiled bytes")
        self.vm.mem_write(text_section.address, self.code)
        return True

    def next_instruction(self, code: bytes, addr: int) -> Optional[cemu.arch.Instruction]:
        """
        Returns a string disassembly of the first instruction from `code`.
        """
        for insn in cemu.arch.disassemble(code, 1, addr):
            return insn

        return None

    def hook_code(self, emu: unicorn.Uc, address: int, size: int, user_data: Any) -> bool:
        """
        Unicorn instruction hook
        """
        if not cemu.const.DEBUG:
            return False

        if not self.vm:
            return False

        code = self.vm.mem_read(address, size)
        insn = self.next_instruction(code, address)
        assert isinstance(insn, cemu.arch.Instruction)

        if self.use_step_mode:
            dbg(f"[vm::runtime] Stepping @ {insn}")
        else:
            dbg(f"[vm::runtime] Executing @ {insn}")
        return True

    def hook_block(self, emu: unicorn.Uc, addr: int, size: int, misc: Any) -> int:
        """
        Unicorn block change hook
        """
        dbg(f"[vm::runtime] Entering block at {addr:#x}")
        return 0

    def hook_interrupt(self, emu: unicorn.Uc, intno: int, data: Any) -> None:
        """
        Unicorn interrupt hook
        """
        dbg(f"[vm::runtime] Triggering interrupt #{intno:d}")
        return

    def hook_syscall(self, emu: unicorn.Uc, data: Any) -> int:
        """
        Unicorn syscall hook
        """
        dbg("[vm::runtime] Syscall")
        return 0

    def hook_mem_access(
        self,
        emu: unicorn.Uc,
        access: int,
        address: int,
        size: int,
        value: int,
        extra: Any,
    ) -> None:
        if access == unicorn.UC_MEM_WRITE:
            info(f"Write: *{address:#x} = {value:#x} (size={size})")
        elif access == unicorn.UC_MEM_READ:
            info(f"Read: *{address:#x} (size={size})")
        return

    def teardown(self) -> None:
        """
        Stops the unicorn environment
        """
        if not self.vm:
            return

        info(f"Ending emulation context at {self.pc():#x}")

        for section in self.sections:
            dbg(f"[vm::teardown] Unmapping {section}")
            self.vm.mem_unmap(section.address, section.size)

        dbg(f"[vm::teardown] Deleting {self.vm}")
        del self.vm
        self.vm = None
        return

    def find_section(self, section_name: str) -> MemorySection:
        """Lookup a particular section by its name

        Args:
            section_name (str): the name of the sections to search

        Raises:
            KeyError: if `section_name` not found

        Returns:
            MemorySection: _description_
        """
        matches = [section for section in self.sections if section.name == section_name]
        if not matches:
            raise KeyError(f"Section '{section_name}' not found")

        if len(matches) > 1:
            raise ValueError(f"Too many sections named {section_name}")

        return matches[0]

    def add_state_change_cb(self, new_state: EmulatorState, cb: Callable) -> None:
        """Register a callback triggered when the emulator switches to a new state

        Args:
            new_state (EmulatorState): the new state
            cb (Callable): the callback to execute when that happens
        """

        self.__state_change_callbacks[new_state].append(cb)
        return

    def set(self, new_state: EmulatorState):
        """Set the new state of the emulator, and invoke the associated callbacks

        Args:
            new_state (EmulatorState): the new state
        """

        if self.state == new_state:
            return

        dbg(f"Emulator state transition: {self.state.name} -> {new_state.name}")

        #
        # Validate which state we're entering
        #
        match new_state:
            case EmulatorState.RUNNING | EmulatorState.IDLE:
                #
                # Make sure there's always an emulation environment ready
                #
                try:
                    self.setup()
                except Exception as e:
                    popup(str(e), PopupType.Error, "Emulator setup error")

                #
                # If we stopped from execution (i.e RUNNING -> [IDLE,FINISHED]), refresh registers
                #
                if self.state == EmulatorState.RUNNING:
                    self.__refresh_registers_from_vm()

            case EmulatorState.FINISHED:
                self.__refresh_registers_from_vm()

            case _:
                pass

        #
        # Do the state change
        #
        info(f"Emulator is now {new_state.name}")
        self.state = new_state

        dbg(f"Executing {len(self.__state_change_callbacks[new_state])} callbacks for state {new_state.name}")

        #
        # Notify the components who've subscribed to the new state change
        #
        for new_state_cb in self.__state_change_callbacks[new_state]:
            function_name = f"{new_state_cb.__module__}.{new_state_cb.__class__.__qualname__}.{new_state_cb.__name__}"
            res = new_state_cb()
            dbg(f"{function_name}() return {res}")

        match self.state:
            case EmulatorState.RUNNING:
                #
                # This will effectively trigger the execution in unicorn
                #
                assert self.threaded_runner, "No threaded runner defined"
                assert callable(getattr(self.threaded_runner, "run")), "Threaded runner is not runnable"
                self.threaded_runner.run()  # type: ignore

            case EmulatorState.TEARDOWN:
                #
                # When the execution is finished, cleanup and switch back to a "NotRunning" state
                # This is done to make sure all the callback can still access the VM
                #
                self.teardown()

                #
                # Completely reset the emulation envionment, and set the status to NOT_RUNNING
                #
                self.reset()

            case _:
                pass

        return

    @property
    def is_running(self) -> bool:
        return self.state in (
            EmulatorState.RUNNING,
            EmulatorState.IDLE,
        )

    def set_threaded_runner(self, runnable_object: object):
        self.threaded_runner = runnable_object
        return

    def read(self, address: int, size: int) -> Optional[bytearray]:
        """Public wrapper for `vm.mem_read`

        Args:
            address (int): _description_
            size (int): _description_

        Returns:
            bytearray:
            None:
        """
        if not self.vm or not self.is_running:
            return None

        return self.vm.mem_read(address, size)

    def write(self, address: int, data: bytes) -> Optional[int]:
        """Public wrapper for `vm.mem_write`

        Args:
            address (int): _description_
            data (bytes): _description_

        Returns:
            bytearray:
            None:
        """
        if not self.vm or not self.is_running:
            return None

        self.vm.mem_write(address, data)
        return len(data)

    def start(self, start_address: int, end_address: int) -> None:
        """Public wrapper for `vm.emu_start`

        Args:

        Returns:
            bytearray:
            None:
        """
        assert self.vm
        assert self.is_running

        with self.lock:
            self.vm.emu_start(start_address, end_address)

        return

    def stop(self) -> None:
        """Public wrapper for `vm.emu_stop`

        Args:

        Returns:
            bytearray:
            None:
        """
        assert self.vm
        assert self.is_running

        self.vm.emu_stop()
        return
