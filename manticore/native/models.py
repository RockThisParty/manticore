"""
Models here are intended to be passed to :meth:`~manticore.native.state.State.invoke_model`, not invoked directly.
"""

from .cpu.abstractcpu import ConcretizeArgument
from .state import State
from ..core.smtlib import issymbolic, BitVec
from ..core.smtlib.solver import Z3Solver
from ..core.smtlib.operators import ITEBV, ZEXTEND
from typing import Union

VARIADIC_FUNC_ATTR = "_variadic"


def isvariadic(model):
    """
    :param callable model: Function model
    :return: Whether `model` models a variadic function
    :rtype: bool
    """
    return getattr(model, VARIADIC_FUNC_ATTR, False)


def variadic(func):
    """
    A decorator used to mark a function model as variadic. This function should
    take two parameters: a :class:`~manticore.native.state.State` object, and
    a generator object for the arguments.

    :param callable func: Function model
    """
    setattr(func, VARIADIC_FUNC_ATTR, True)
    return func


def _find_zero(cpu, constrs, ptr):
    """
    Helper for finding the closest NULL or, effectively NULL byte from a starting address.

    :param Cpu cpu:
    :param ConstraintSet constrs: Constraints for current `State`
    :param int ptr: Address to start searching for a zero from
    :return: Offset from `ptr` to first byte that is 0 or an `Expression` that must be zero
    """

    offset = 0
    while True:
        byt = cpu.read_int(ptr + offset, 8)

        if issymbolic(byt):
            if not Z3Solver.instance().can_be_true(constrs, byt != 0):
                break
        else:
            if byt == 0:
                break

        offset += 1

    return offset


def _find_zeros(cpu, constrs, ptr):
    """
    Helper for finding all bytes that can be NULL until one is found that is NULL or is effectively NULL from the starting address.

    :param Cpu cpu:
    :param ConstraintSet constrs: Constraints for current `State`
    :param int ptr: Address to start searching for a zero from
    :return: Offset from `ptr` to first byte that is 0 or an `Expression` that must be zero
    :return: List of offsets from `ptr` to a byte that can be 0. The last value in the
    list is the offset to the first byte found that is 0 or an `Expression` that must be 0
    """

    offset = 0
    can_be_zero = []
    while True:
        byt = cpu.read_int(ptr + offset, 8)

        if issymbolic(byt):
            # If the byte can be 0 append the offset location to can_be_zero
            if Z3Solver.instance().can_be_true(constrs, byt == 0):
                can_be_zero.append(offset)
            # If it is not the case that there exists another possible val for byt than 0
            # (Byt is constrained to 0) then an effectively NULL byte has been found
            if not Z3Solver.instance().can_be_true(constrs, byt != 0):
                break
        else:
            if byt == 0:
                can_be_zero.append(offset)
                break

        offset += 1

    return can_be_zero


def strcmp(state: State, s1: Union[int, BitVec], s2: Union[int, BitVec]):
    """
    strcmp symbolic model.

    Algorithm: Walks from end of string (minimum offset to NULL in either string)
    to beginning building tree of ITEs each time either of the
    bytes at current offset is symbolic.

    Points of Interest:
    - We've been building up a symbolic tree but then encounter two
    concrete bytes that differ. We can throw away the entire symbolic
    tree!
    - If we've been encountering concrete bytes that match
    at the end of the string as we walk forward, and then we encounter
    a pair where one is symbolic, we can forget about that 0 `ret` we've
    been tracking and just replace it with the symbolic subtraction of
    the two

    :param State state: Current program state
    :param int s1: Address of string 1
    :param int s2: Address of string 2
    :return: Symbolic strcmp result
    :rtype: Expression or int
    """

    cpu = state.cpu

    if issymbolic(s1):
        raise ConcretizeArgument(state.cpu, 1)
    if issymbolic(s2):
        raise ConcretizeArgument(state.cpu, 2)

    s1_zero_idx = _find_zero(cpu, state.constraints, s1)
    s2_zero_idx = _find_zero(cpu, state.constraints, s2)
    min_zero_idx = min(s1_zero_idx, s2_zero_idx)

    ret = None

    for offset in range(min_zero_idx, -1, -1):
        s1char = ZEXTEND(cpu.read_int(s1 + offset, 8), cpu.address_bit_size)
        s2char = ZEXTEND(cpu.read_int(s2 + offset, 8), cpu.address_bit_size)

        if issymbolic(s1char) or issymbolic(s2char):
            if ret is None or (not issymbolic(ret) and ret == 0):
                ret = s1char - s2char
            else:
                ret = ITEBV(cpu.address_bit_size, s1char != s2char, s1char - s2char, ret)
        else:
            if s1char != s2char:
                ret = s1char - s2char
            elif ret is None:
                ret = 0

    return ret


def strlen(state: State, s: Union[int, BitVec]):
    """
    strlen symbolic model.

    Algorithm: Walks from end of string not including NULL building ITE tree when current byte is symbolic.

    :param State state: current program state
    :param int s: Address of string
    :return: Symbolic strlen result
    :rtype: Expression or int
    """

    cpu = state.cpu

    if issymbolic(s):
        raise ConcretizeArgument(state.cpu, 1)

    zero_idx = _find_zero(cpu, state.constraints, s)

    ret = zero_idx

    for offset in range(zero_idx - 1, -1, -1):
        byt = cpu.read_int(s + offset, 8)
        if issymbolic(byt):
            ret = ITEBV(cpu.address_bit_size, byt == 0, offset, ret)

    return ret


def is_NULL(byte, constrs) -> bool:
    """
    Checks if a given byte read from memory is NULL or effectively NULL

    :param byte: byte read from memory to be examined
    :param constrs: state constraints
    :return: whether a given byte is NULL or constrained to NULL
    """
    if issymbolic(byte):
        return not Z3Solver.instance().can_be_true(constrs, byte != 0)
    else:
        return byte == 0


def not_NULL(byte, constrs) -> bool:
    """
    Checks if a given byte read from memory is not NULL or cannot be NULL

    :param byte: byte read from memory to be examined
    :param constrs: state constraints
    :return: whether a given byte is not NULL or cannot be NULL
    """
    if issymbolic(byte):
        return not Z3Solver.instance().can_be_true(constrs, byte == 0)
    else:
        return byte != 0


def strcpy(state: State, dst: Union[int, BitVec], src: Union[int, BitVec]) -> Union[int, BitVec]:
    """
    strcpy symbolic model

    Algorithm: Copy every byte from the src to dst until finding a byte that can be or is NULL.
    If the byte is NULL or is constrained to only the NULL value, append the NULL value to dst
    and return. If the value can be NULL or another value write an `Expression` for every following
    byte that sets a value to the src or dst byte according to the preceding bytes until a NULL
    byte or effectively NULL byte is found.

    :param state: current program state
    :param dst: destination string address
    :param src: source string address
    :return: pointer to the dst
    """
    if issymbolic(src):
        raise ConcretizeArgument(state.cpu, 1)

    if issymbolic(dst):
        raise ConcretizeArgument(state.cpu, 2)

    cpu = state.cpu
    constrs = state.constraints
    ret = dst
    c = cpu.read_int(src, 8)
    # Copy until '\000' is reached or symbolic memory that can be '\000'
    while not_NULL(c, constrs):
        cpu.write_int(dst, c, 8)
        src += 1
        dst += 1
        c = cpu.read_int(src, 8)

    # If the byte is symbolic and constrained to '\000' or is '\000' write concrete val and return
    if is_NULL(c, constrs):
        cpu.write_int(dst, 0, 8)
        return ret

    zeros = _find_zeros(cpu, constrs, src)
    null = zeros[-1]
    # If the symbolic byte was not constrained to '\000' write the appropriate symbolic bytes
    for offset in range(null, -1, -1):
        src_val = cpu.read_int(src + offset, 8)
        dst_val = cpu.read_int(dst + offset, 8)
        if zeros[-1] == offset:
            # Make sure last byte of the copy is always a concrete '\000'
            src_val = ITEBV(8, src_val != 0, src_val, 0)
            zeros.pop()

        # For every byte that could be null before the current byte add an
        # if then else case to the bitvec tree to set the value to the src or dst byte accordingly
        for zero in reversed(zeros):
            c = cpu.read_int(src + zero, 8)
            src_val = ITEBV(8, c != 0, src_val, dst_val)
        cpu.write_int(dst + offset, src_val, 8)

    return ret
