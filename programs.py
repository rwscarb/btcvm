# Sample programs for the BTC-clocked VM.
# Registers: R0-R7. Opcodes: LOAD, ADD, SUB, MUL, JMP, JZ, HALT.

# Count down from 100 to 0.
COUNTDOWN = [
    ('LOAD', 0, 100),  # R0 = 100
    ('LOAD', 1, 1),    # R1 = 1
    # pc=2: loop
    ('JZ',  0, 5),     # if R0 == 0: goto HALT
    ('SUB', 0, 0, 1),  # R0 = R0 - 1
    ('JMP', 2),        # goto loop
    ('HALT',),         # pc=5
]

# Fibonacci: R0=F(n-2), R1=F(n-1), R2=temp, R3=step counter
# Runs until R3 reaches 0 (20 iterations).
FIBONACCI = [
    ('LOAD', 0, 0),    # R0 = F(0)
    ('LOAD', 1, 1),    # R1 = F(1)
    ('LOAD', 3, 20),   # R3 = 20 (iterations)
    ('LOAD', 4, 1),    # R4 = 1 (decrement)
    # pc=4: loop
    ('JZ',  3, 9),     # if R3 == 0: goto HALT
    ('ADD', 2, 0, 1),  # R2 = R0 + R1
    ('ADD', 0, 1, 4),  # R0 = R1 + 0 ... cheat: MOV R0←R1 via ADD R0,R1,R4-R4
    # Simpler: just shift
    ('LOAD', 0, 0),    # placeholder — see below
    ('HALT',),         # pc=8
]

# Simpler fibonacci using only what the VM supports:
# R0=a, R1=b, R2=temp, R3=iters, R4=1
FIBONACCI = [
    ('LOAD', 0, 0),    # R0 = 0 (a)
    ('LOAD', 1, 1),    # R1 = 1 (b)
    ('LOAD', 3, 20),   # R3 = 20 iterations
    ('LOAD', 4, 1),    # R4 = 1
    # pc=4: loop
    ('JZ',  3, 10),    # if R3 == 0: halt
    ('ADD', 2, 0, 1),  # R2 = a + b  (next fib)
    ('ADD', 0, 1, 3),  # R0 = b  (we can't MOV, so use: R0 = R1 + 0)
    # Hack: zero out R5, use ADD R0, R1, R5
    # Actually let's use SUB: R0 = R1 - 0
    # We need a zero register. Use R7=0 always.
    ('LOAD', 7, 0),    # R7 = 0 (zero register)
    ('ADD', 0, 1, 7),  # R0 = R1 + 0  (MOV R0 ← R1)
    ('ADD', 1, 2, 7),  # R1 = R2 + 0  (MOV R1 ← R2)
    ('SUB', 3, 3, 4),  # R3 = R3 - 1
    ('JMP', 4),        # goto loop
    ('HALT',),         # pc=12
]

# Clean version without the commented-out draft above:
FIBONACCI = [
    ('LOAD', 0, 0),    # pc=0  R0=a=0
    ('LOAD', 1, 1),    # pc=1  R1=b=1
    ('LOAD', 3, 20),   # pc=2  R3=20 (iters)
    ('LOAD', 4, 1),    # pc=3  R4=1
    ('LOAD', 7, 0),    # pc=4  R7=0 (zero)
    # loop at pc=5
    ('JZ',  3, 11),    # pc=5  if R3==0 goto HALT
    ('ADD', 2, 0, 1),  # pc=6  R2 = R0+R1
    ('ADD', 0, 1, 7),  # pc=7  R0 = R1
    ('ADD', 1, 2, 7),  # pc=8  R1 = R2
    ('SUB', 3, 3, 4),  # pc=9  R3 = R3-1
    ('JMP', 5),        # pc=10 goto loop
    ('HALT',),         # pc=11
]

PROGRAMS = {
    'countdown': COUNTDOWN,
    'fibonacci': FIBONACCI,
}
