import os
import random


BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

SIGNAL_FILE = os.path.join(
    BASE_DIR,
    "adaptation",
    ".adapt_signal"
)


# =============================================================
# AFL++ lifecycle
# =============================================================

def init(seed):

    random.seed(seed)


def deinit():

    pass


# =============================================================
# Read adaptation signal
# =============================================================

def read_adaptation_mode():

    try:

        if not os.path.isfile(
            SIGNAL_FILE
        ):

            return "DEFAULT"

        with open(
            SIGNAL_FILE,
            "r"
        ) as file:

            content = file.read().strip()

        if not content:

            return "DEFAULT"

        if "MODE=" not in content:

            return "DEFAULT"

        mode = content.split(
            "MODE=",
            1
        )[1]

        if ";" in mode:

            mode = mode.split(
                ";",
                1
            )[0]

        valid_modes = {

            "DEFAULT",

            "EXPLORE_HEAVY",

            "THROTTLE_POWER",

            "REDUCE_MUTATION_SIZE",

            "STRONG_THROTTLE",

            "SKIP_TIMEOUTS",

            "EXPLOIT_CRASH"
        }

        if mode not in valid_modes:

            return "DEFAULT"

        return mode

    except Exception:

        return "DEFAULT"


# =============================================================
# Basic mutation operations
# =============================================================

def bit_flip(buf):

    if not buf:

        return buf

    mutated = bytearray(buf)

    index = random.randrange(
        len(mutated)
    )

    mutated[index] ^= 1

    return mutated


def byte_flip(buf):

    if not buf:

        return buf

    mutated = bytearray(buf)

    index = random.randrange(
        len(mutated)
    )

    mutated[index] ^= 0xFF

    return mutated


def random_byte(buf):

    if not buf:

        return buf

    mutated = bytearray(buf)

    index = random.randrange(
        len(mutated)
    )

    mutated[index] = random.randint(
        0,
        255
    )

    return mutated


def arithmetic_mutation(buf):

    if not buf:

        return buf

    mutated = bytearray(buf)

    index = random.randrange(
        len(mutated)
    )

    change = random.choice(
        [
            -16,
            -8,
            -4,
            -1,
            1,
            4,
            8,
            16
        ]
    )

    mutated[index] = (

        mutated[index]
        +
        change
    ) % 256

    return mutated


def delete_bytes(buf):

    if len(buf) < 2:

        return buf

    mutated = bytearray(buf)

    delete_size = random.randint(

        1,

        min(
            8,
            len(mutated) - 1
        )
    )

    start = random.randint(

        0,

        len(mutated)
        -
        delete_size
    )

    del mutated[
        start:start + delete_size
    ]

    return mutated


def insert_bytes(
    buf,
    max_size
):

    mutated = bytearray(buf)

    if len(mutated) >= max_size:

        return mutated

    insert_size = random.randint(
        1,
        8
    )

    insert_size = min(

        insert_size,

        max_size
        -
        len(mutated)
    )

    position = random.randint(

        0,

        len(mutated)
    )

    for _ in range(
        insert_size
    ):

        mutated.insert(

            position,

            random.randint(
                0,
                255
            )
        )

        position += 1

    return mutated


# =============================================================
# Strategies
# =============================================================

def mutate_default(
    buf,
    max_size
):

    return bit_flip(buf)


# -------------------------------------------------------------
# Coverage slowdown
# -------------------------------------------------------------

def mutate_explore_heavy(
    buf,
    max_size
):

    mutation = random.choice(
        [
            bit_flip,
            byte_flip,
            random_byte,
            arithmetic_mutation,
            delete_bytes
        ]
    )

    return mutation(buf)


# -------------------------------------------------------------
# CPU spike
#
# Keep mutations relatively lightweight.
# -------------------------------------------------------------

def mutate_throttle_power(
    buf,
    max_size
):

    mutation = random.choice(
        [
            bit_flip,
            arithmetic_mutation
        ]
    )

    return mutation(buf)


# -------------------------------------------------------------
# Memory spike
#
# Avoid insertion and large mutations.
# -------------------------------------------------------------

def mutate_reduce_mutation_size(
    buf,
    max_size
):

    if not buf:

        return buf

    mutated = bytearray(buf)

    # Small mutation only
    index = random.randrange(
        len(mutated)
    )

    mutated[index] ^= random.choice(
        [
            1,
            2,
            4
        ]
    )

    return mutated


# -------------------------------------------------------------
# CPU + memory spike
#
# Strongest lightweight throttling.
# -------------------------------------------------------------

def mutate_strong_throttle(
    buf,
    max_size
):

    if not buf:

        return buf

    # Very small mutation.
    mutated = bytearray(buf)

    index = random.randrange(
        len(mutated)
    )

    mutated[index] ^= 1

    return mutated


# -------------------------------------------------------------
# Timeout adaptation
# -------------------------------------------------------------

def mutate_skip_timeouts(
    buf,
    max_size
):

    mutation = random.choice(
        [
            bit_flip,
            arithmetic_mutation
        ]
    )

    return mutation(buf)


# -------------------------------------------------------------
# Crash exploitation
# -------------------------------------------------------------

def mutate_exploit_crash(
    buf,
    max_size
):

    mutation = random.choice(
        [
            bit_flip,
            arithmetic_mutation,
            byte_flip
        ]
    )

    return mutation(buf)


# =============================================================
# AFL++ custom mutation
# =============================================================

def fuzz(
    buf,
    add_buf,
    max_size
):

    if not buf:

        return buf

    mode = read_adaptation_mode()

    # ---------------------------------------------------------
    # Select strategy
    # ---------------------------------------------------------

    if mode == "EXPLORE_HEAVY":

        mutated_out = mutate_explore_heavy(
            buf,
            max_size
        )

    elif mode == "THROTTLE_POWER":

        mutated_out = mutate_throttle_power(
            buf,
            max_size
        )

    elif mode == "REDUCE_MUTATION_SIZE":

        mutated_out = mutate_reduce_mutation_size(
            buf,
            max_size
        )

    elif mode == "STRONG_THROTTLE":

        mutated_out = mutate_strong_throttle(
            buf,
            max_size
        )

    elif mode == "SKIP_TIMEOUTS":

        mutated_out = mutate_skip_timeouts(
            buf,
            max_size
        )

    elif mode == "EXPLOIT_CRASH":

        mutated_out = mutate_exploit_crash(
            buf,
            max_size
        )

    else:

        mutated_out = mutate_default(
            buf,
            max_size
        )

    # ---------------------------------------------------------
    # AFL++ maximum size
    # ---------------------------------------------------------

    if len(mutated_out) > max_size:

        mutated_out = mutated_out[
            :max_size
        ]

    return mutated_out
