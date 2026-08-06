; Count down from 100 to 0.
; R0 = counter, R1 = 1 (decrement)

    LOAD R0, 100
    LOAD R1, 1

loop:
    JZ   R0, done
    SUB  R0, R0, R1
    JMP  loop

done:
    HALT
