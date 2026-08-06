import hashlib
import json

REGISTERS = 8
MEMORY_SIZE = 256


class VM:
    def __init__(self):
        self.registers = [0] * REGISTERS
        self.memory = [None] * MEMORY_SIZE
        self.pc = 0
        self.halted = False
        self.ticks = 0
        self.trace = None  # set to a VMTrace instance to enable step recording

    def load_program(self, program):
        for i, instr in enumerate(program):
            self.memory[i] = instr

    def step(self):
        if self.halted:
            return

        instr = self.memory[self.pc]
        if instr is None:
            self.halted = True
            return

        op = instr[0]
        pc_before = self.pc
        regs_before = self.registers[:] if self.trace is not None else None

        if op == 'LOAD':
            _, r, val = instr
            self.registers[r] = val
            self.pc += 1
        elif op == 'ADD':
            _, dst, a, b = instr
            self.registers[dst] = self.registers[a] + self.registers[b]
            self.pc += 1
        elif op == 'SUB':
            _, dst, a, b = instr
            self.registers[dst] = self.registers[a] - self.registers[b]
            self.pc += 1
        elif op == 'MUL':
            _, dst, a, b = instr
            self.registers[dst] = self.registers[a] * self.registers[b]
            self.pc += 1
        elif op == 'JMP':
            _, addr = instr
            self.pc = addr
        elif op == 'JZ':
            _, r, addr = instr
            self.pc = addr if self.registers[r] == 0 else self.pc + 1
        elif op == 'HALT':
            self.halted = True
        else:
            raise ValueError(f"Unknown opcode: {op}")

        self.ticks += 1

        if self.trace is not None:
            self.trace.record(pc_before, op, regs_before, self.registers[:])

    def run(self, max_steps=None):
        steps = 0
        while not self.halted:
            self.step()
            steps += 1
            if max_steps is not None and steps >= max_steps:
                break
        return steps

    def state_hash(self):
        state = {
            'pc': self.pc,
            'ticks': self.ticks,
            'halted': self.halted,
            'registers': self.registers[:],
        }
        return hashlib.sha256(
            json.dumps(state, sort_keys=True).encode()
        ).hexdigest()

    def __repr__(self):
        regs = ' '.join(f'R{i}={v}' for i, v in enumerate(self.registers))
        return f"VM(pc={self.pc} ticks={self.ticks} halted={self.halted} [{regs}])"
