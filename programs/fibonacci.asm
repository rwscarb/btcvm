; Compute Fibonacci numbers. Runs 20 iterations.
; On exit: R0 = F(20) = 6765, R1 = F(21) = 10946
;
; Registers:
;   R0 = a (current)
;   R1 = b (next)
;   R2 = temp
;   R3 = iterations remaining
;   R4 = 1  (decrement)
;   R7 = 0  (zero register, used for MOV via ADD)

    LOAD R0, 0
    LOAD R1, 1
    LOAD R3, 20
    LOAD R4, 1
    LOAD R7, 0

loop:
    JZ   R3, done
    ADD  R2, R0, R1   ; temp = a + b
    ADD  R0, R1, R7   ; a = b
    ADD  R1, R2, R7   ; b = temp
    SUB  R3, R3, R4   ; iters--
    JMP  loop

done:
    HALT
