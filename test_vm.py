import pytest
from vm import VM
from programs import PROGRAMS
from vdf import VDF
from trace import VMTrace
from fleet import VMFleet


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


# --- VDF ---

SEED = 'a' * 64  # valid 64-char hex string
FAST_STEPS = 10  # tiny step count for test speed


def test_vdf_bad_seed_raises():
    with pytest.raises(ValueError):
        VDF('not64chars')

def test_vdf_tick_returns_hex_pair():
    vdf = VDF(SEED, steps_per_tick=FAST_STEPS)
    inp, out = vdf.tick()
    assert len(inp) == 64 and len(out) == 64
    int(inp, 16)
    int(out, 16)

def test_vdf_tick_changes_state():
    vdf = VDF(SEED, steps_per_tick=FAST_STEPS)
    _, out1 = vdf.tick()
    _, out2 = vdf.tick()
    assert out1 != out2

def test_vdf_tick_count_increments():
    vdf = VDF(SEED, steps_per_tick=FAST_STEPS)
    vdf.tick()
    vdf.tick()
    assert vdf.tick_count == 2

def test_vdf_verify_valid():
    vdf = VDF(SEED, steps_per_tick=FAST_STEPS)
    inp, out = vdf.tick()
    assert VDF.verify(inp, out, steps=FAST_STEPS)

def test_vdf_verify_wrong_output():
    vdf = VDF(SEED, steps_per_tick=FAST_STEPS)
    inp, out = vdf.tick()
    bad_out = 'b' * 64
    assert not VDF.verify(inp, bad_out, steps=FAST_STEPS)

def test_vdf_chain_is_sequential():
    vdf = VDF(SEED, steps_per_tick=FAST_STEPS)
    inp1, out1 = vdf.tick()
    inp2, out2 = vdf.tick()
    assert inp2 == out1  # output of tick N is input of tick N+1

def test_vdf_verify_chain_valid():
    vdf = VDF(SEED, steps_per_tick=FAST_STEPS)
    ticks = []
    for _ in range(3):
        inp, out = vdf.tick()
        ticks.append({'vdf_input': inp, 'vdf_hash': out})
    assert VDF.verify_chain(ticks, steps=FAST_STEPS)

def test_vdf_verify_chain_tampered():
    vdf = VDF(SEED, steps_per_tick=FAST_STEPS)
    ticks = []
    for _ in range(3):
        inp, out = vdf.tick()
        ticks.append({'vdf_input': inp, 'vdf_hash': out})
    ticks[1]['vdf_hash'] = 'c' * 64  # tamper middle entry
    assert not VDF.verify_chain(ticks, steps=FAST_STEPS)


# --- VMTrace ---

def test_trace_records_steps():
    vm = fresh('fibonacci')
    vm.trace = VMTrace()
    vm.run(max_steps=10)
    assert vm.trace.step_count() == 10

def test_trace_tip_is_hex():
    vm = fresh('countdown')
    vm.trace = VMTrace()
    vm.run(max_steps=5)
    tip = vm.trace.tip_hash()
    assert len(tip) == 64
    int(tip, 16)

def test_trace_merkle_root_is_deterministic():
    vm1 = fresh('fibonacci')
    vm1.trace = VMTrace()
    vm1.run(max_steps=15)

    vm2 = fresh('fibonacci')
    vm2.trace = VMTrace()
    vm2.run(max_steps=15)

    assert vm1.trace.merkle_root() == vm2.trace.merkle_root()

def test_trace_merkle_root_changes_with_execution():
    vm = fresh('fibonacci')
    vm.trace = VMTrace()
    vm.run(max_steps=5)
    r1 = vm.trace.merkle_root()
    vm.run(max_steps=5)
    r2 = vm.trace.merkle_root()
    assert r1 != r2

def test_trace_empty_root():
    t = VMTrace()
    assert t.merkle_root() == '0' * 64

def test_trace_export_and_verify(tmp_path):
    vm = fresh('fibonacci')
    vm.trace = VMTrace()
    vm.run(max_steps=20)
    trace_file = str(tmp_path / 'trace.jsonl')
    vm.trace.export(trace_file)
    ok, root = VMTrace.verify_file(trace_file)
    assert ok
    assert root == vm.trace.merkle_root()

def test_trace_verify_tampered(tmp_path):
    import json
    vm = fresh('fibonacci')
    vm.trace = VMTrace()
    vm.run(max_steps=20)
    trace_file = str(tmp_path / 'trace.jsonl')
    vm.trace.export(trace_file)

    # Tamper with step 5
    lines = open(trace_file).readlines()
    step = json.loads(lines[5])
    step['regs_after'][0] = 9999
    lines[5] = json.dumps(step) + '\n'
    with open(trace_file, 'w') as f:
        f.writelines(lines)

    ok, _ = VMTrace.verify_file(trace_file)
    assert not ok

def test_trace_does_not_affect_execution():
    vm_no_trace = fresh('fibonacci')
    vm_no_trace.run()

    vm_trace = fresh('fibonacci')
    vm_trace.trace = VMTrace()
    vm_trace.run()

    assert vm_no_trace.registers == vm_trace.registers
    assert vm_no_trace.ticks == vm_trace.ticks
    assert vm_no_trace.state_hash() == vm_trace.state_hash()


# --- VMFleet ---

def test_fleet_size():
    f = VMFleet(['fibonacci', 'countdown', 'fibonacci'])
    assert f.size == 3

def test_fleet_unknown_program():
    with pytest.raises(ValueError):
        VMFleet(['fibonacci', 'badprog'])

def test_fleet_run_tick_returns_cycles():
    f = VMFleet(['fibonacci', 'countdown'])
    cycles = f.run_tick(max_steps=5)
    assert len(cycles) == 2
    assert all(c == 5 for c in cycles)

def test_fleet_all_halted_initially_false():
    f = VMFleet(['fibonacci', 'countdown'])
    assert not f.all_halted()

def test_fleet_all_halted_after_full_run():
    f = VMFleet(['countdown', 'fibonacci'])
    while not f.all_halted():
        f.run_tick(max_steps=50)
    assert f.all_halted()

def test_fleet_vm_commitments_length():
    f = VMFleet(['fibonacci', 'countdown', 'fibonacci'])
    f.run_tick(max_steps=10)
    assert len(f.vm_commitments()) == 3

def test_fleet_root_is_hex64():
    f = VMFleet(['fibonacci', 'countdown'])
    f.run_tick(max_steps=10)
    root = f.fleet_root()
    assert len(root) == 64
    int(root, 16)

def test_fleet_root_deterministic():
    f1 = VMFleet(['fibonacci', 'countdown'])
    f1.run_tick(max_steps=10)

    f2 = VMFleet(['fibonacci', 'countdown'])
    f2.run_tick(max_steps=10)

    assert f1.fleet_root() == f2.fleet_root()

def test_fleet_root_changes_after_tick():
    f = VMFleet(['fibonacci', 'countdown'])
    f.run_tick(max_steps=5)
    r1 = f.fleet_root()
    f.run_tick(max_steps=5)
    r2 = f.fleet_root()
    assert r1 != r2

def test_fleet_root_differs_from_any_single_vm():
    f = VMFleet(['fibonacci', 'countdown'])
    f.run_tick(max_steps=10)
    root = f.fleet_root()
    for commit in f.vm_commitments():
        assert root != commit

def test_fleet_snapshots_shape():
    f = VMFleet(['fibonacci', 'countdown'])
    cycles = f.run_tick(max_steps=10)
    snaps = f.snapshots(cycles)
    assert len(snaps) == 2
    for i, snap in enumerate(snaps):
        assert snap['vm_id'] == i
        assert 'state_hash' in snap
        assert 'registers' in snap
        assert 'cycles' in snap

def test_fleet_verify_fleet_root():
    f = VMFleet(['fibonacci', 'countdown'])
    cycles = f.run_tick(max_steps=10)
    entry = {
        'fleet_size': f.size,
        'fleet_root': f.fleet_root(),
        'vms': f.snapshots(cycles),
    }
    assert VMFleet.verify_fleet_root(entry)

def test_fleet_verify_fleet_root_tampered():
    f = VMFleet(['fibonacci', 'countdown'])
    cycles = f.run_tick(max_steps=10)
    snaps = f.snapshots(cycles)
    snaps[0]['state_hash'] = 'a' * 64  # tamper
    entry = {
        'fleet_size': f.size,
        'fleet_root': f.fleet_root(),
        'vms': snaps,
    }
    assert not VMFleet.verify_fleet_root(entry)

def test_fleet_with_trace():
    f = VMFleet(['fibonacci', 'countdown'], enable_trace=True)
    cycles = f.run_tick(max_steps=10)
    snaps = f.snapshots(cycles)
    for snap in snaps:
        assert 'trace_root' in snap
        assert len(snap['trace_root']) == 64

def test_fleet_trace_root_used_in_commitment():
    f_trace = VMFleet(['fibonacci'], enable_trace=True)
    f_trace.run_tick(max_steps=10)
    root_trace = f_trace.fleet_root()

    f_no_trace = VMFleet(['fibonacci'], enable_trace=False)
    f_no_trace.run_tick(max_steps=10)
    root_no_trace = f_no_trace.fleet_root()

    # Trace Merkle root != state_hash → fleet roots differ
    assert root_trace != root_no_trace

def test_fleet_single_vm_matches_standalone():
    # Fleet of 1 with no trace should produce the same state_hash as a plain VM
    f = VMFleet(['fibonacci'])
    f.run_tick(max_steps=15)

    vm = VM()
    vm.load_program(PROGRAMS['fibonacci'])
    vm.run(max_steps=15)

    assert f.vm_commitments()[0] == vm.state_hash()
