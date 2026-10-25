#pragma once
#include <setjmp.h>
extern _Thread_local jmp_buf ldac_exception;
extern _Thread_local int ldac_input_length;
#define LDAC_REQUIRE(condition) do { if (!(condition)) longjmp(ldac_exception, 1); } while (0)
