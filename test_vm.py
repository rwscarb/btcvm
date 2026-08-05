import pytest
from vm import VM
from programs import PROGRAMS


def fresh(program_name):
    vm = VM()
    vm.load_program(PROGRAMS[program_name])
    return vm


# --- countdown ---

def test_countdown_halts():
    vm = fresh('countdown')
    vm.run()
    assert vm.halted

def test_countdown_result():
    vm = fresh('countdown')
    vm.run()
    assert vm.registers[0] == 0

def test_countdown_tick_count():
    vm = fresh('countdown')
    vm.run()
    # 100 iterations × 3 instructions (JZ + SUB + JMP) + final JZ + HALT = 304
    assert vm.ticks == 304


# --- fibonacci ---

def test_fibonacci_halts():
    vm = fresh('fibonacci')
    vm.run()
    assert vm.halted

def test_fibonacci_result():
    vm = fresh('fibonacci')
    vm.run()
    assert vm.registers[0] == 6765   # F(20)
    assert vm.registers[1] == 10946  # F(21)


# --- partial execution ---

def test_partial_run_does_not_halt():
    vm = fresh('fibonacci')
    cycles = vm.run(max_steps=10)
    assert cycles == 10
    assert not vm.halted

def test_partial_then_complete():
    vm = fresh('countdown')
    vm.run(max_steps=50)
    assert not vm.halted
    vm.run()  # finish
    assert vm.halted
    assert vm.registers[0] == 0


# --- state hash ---

def test_state_hash_is_deterministic():
    vm = fresh('fibonacci')
    vm.run(max_steps=20)
    assert vm.state_hash() == vm.state_hash()

def test_state_hash_changes_with_execution():
    vm = fresh('fibonacci')
    h1 = vm.state_hash()
    vm.run(max_steps=5)
    h2 = vm.state_hash()
    assert h1 != h2

def test_state_hash_is_hex():
    vm = fresh('fibonacci')
    h = vm.state_hash()
    assert len(h) == 64
    int(h, 16)  # raises ValueError if not valid hex


# --- edge cases ---

def test_halt_is_idempotent():
    vm = fresh('countdown')
    vm.run()
    ticks_after_halt = vm.ticks
    vm.step()  # should be a no-op
    assert vm.ticks == ticks_after_halt
    assert vm.halted

def test_unknown_opcode_raises():
    vm = VM()
    vm.load_program([('BADOP', 0, 1)])
    with pytest.raises(ValueError):
        vm.step()
