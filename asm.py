"""
Minimal two-pass assembler for the btcvm instruction set.

Syntax
------
  ; or # starts a line comment (inline too)
  Labels end with a colon, either alone or prefixed to an instruction:
      loop:
          JZ R3, done
      done: HALT
  Registers: R0–R7 (case-insensitive)
  Immediates: integer literals

Opcodes
-------
  LOAD  Rdst, imm
  ADD   Rdst, Ra, Rb
  SUB   Rdst, Ra, Rb
  MUL   Rdst, Ra, Rb
  JMP   label
  JZ    Rtest, label
  HALT
"""

import re
import os
import glob


_REG = re.compile(r'^R([0-7])$', re.IGNORECASE)
_SPLIT = re.compile(r'[\s,]+')


def _reg(tok: str) -> int:
    m = _REG.match(tok.strip())
    if not m:
        raise SyntaxError(f"expected register R0-R7, got {tok!r}")
    return int(m.group(1))


def _imm(tok: str) -> int:
    try:
        return int(tok.strip())
    except ValueError:
        raise SyntaxError(f"expected integer immediate, got {tok!r}")


def assemble(source: str) -> list[tuple]:
    """Assemble source text into a list of VM instruction tuples."""

    # Pre-process: strip comments, split into (label | None, instruction | None) pairs
    raw_lines: list[tuple[str | None, str | None]] = []
    for line in source.splitlines():
        line = re.sub(r'[;#].*', '', line).strip()
        if not line:
            continue
        if ':' in line:
            label_part, _, rest = line.partition(':')
            raw_lines.append((label_part.strip() or None, rest.strip() or None))
        else:
            raw_lines.append((None, line))

    # Pass 1: assign a PC to each label
    labels: dict[str, int] = {}
    pc = 0
    for label, instr in raw_lines:
        if label is not None:
            labels[label] = pc
        if instr:
            pc += 1

    # Pass 2: emit instruction tuples
    program: list[tuple] = []
    for label, instr in raw_lines:
        if not instr:
            continue
        parts = _SPLIT.split(instr.strip())
        op = parts[0].upper()
        args = parts[1:]

        if op == 'LOAD':
            program.append(('LOAD', _reg(args[0]), _imm(args[1])))
        elif op in ('ADD', 'SUB', 'MUL'):
            program.append((op, _reg(args[0]), _reg(args[1]), _reg(args[2])))
        elif op == 'JMP':
            target = args[0].strip()
            if target not in labels:
                raise SyntaxError(f"undefined label {target!r}")
            program.append(('JMP', labels[target]))
        elif op == 'JZ':
            target = args[1].strip()
            if target not in labels:
                raise SyntaxError(f"undefined label {target!r}")
            program.append(('JZ', _reg(args[0]), labels[target]))
        elif op == 'HALT':
            program.append(('HALT',))
        else:
            raise SyntaxError(f"unknown opcode {op!r}")

    return program


def assemble_file(path: str) -> list[tuple]:
    with open(path) as f:
        return assemble(f.read())


def discover(directory: str | None = None) -> dict[str, list[tuple]]:
    """Load and assemble every .asm file in *directory* (default: programs/)."""
    if directory is None:
        directory = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'programs')
    result: dict[str, list[tuple]] = {}
    for path in sorted(glob.glob(os.path.join(directory, '*.asm'))):
        name = os.path.splitext(os.path.basename(path))[0]
        result[name] = assemble_file(path)
    return result
