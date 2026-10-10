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

LAST_MODE = None


# =============================================================
# AFL++ lifecycle
# =============================================================

def init(seed):

    random.seed(seed)


def deinit():

    pass


# We do not use add_buf in fuzz().
# Tell AFL++ that splice data is unnecessary.
def splice_optout():

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

    bit = 1 << random.randrange(
        8
    )

    mutated[index] ^= bit

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

    if insert_size <= 0:

        return mutated

    position = random.randint(
        0,
        len(mutated)
    )

    new_bytes = bytearray(
        random.randint(
            0,
            255
        )
        for _ in range(
            insert_size
        )
    )

    mutated[
        position:position
    ] = new_bytes

    return mutated


# =============================================================
# Strategies
# =============================================================

# -------------------------------------------------------------
# DEFAULT
#
# Normally the custom-mutator stage is disabled through
# fuzz_count(), so this function is mainly a fallback.
# -------------------------------------------------------------

def mutate_default(
    buf,
    max_size
):

    return bit_flip(
        buf
    )


# -------------------------------------------------------------
# Coverage slowdown
#
# Increase mutation diversity temporarily to try to escape a
# coverage plateau.
#
# Unlike the previous version, EXPLORE_HEAVY performs two small
# mutation operations and may use insertion.
# -------------------------------------------------------------

def mutate_explore_heavy(
    buf,
    max_size
):

    mutated = bytearray(
        buf
    )

    # Two mutation operations provide stronger exploration
    # without making the intervention excessively expensive.
    mutation_count = 2

    for _ in range(
        mutation_count
    ):

        mutation_name = random.choice(
            [
                "bit_flip",
                "byte_flip",
                "random_byte",
                "arithmetic",
                "delete",
                "insert"
            ]
        )

        if mutation_name == "bit_flip":

            mutated = bit_flip(
                mutated
            )

        elif mutation_name == "byte_flip":

            mutated = byte_flip(
                mutated
            )

        elif mutation_name == "random_byte":

            mutated = random_byte(
                mutated
            )

        elif mutation_name == "arithmetic":

            mutated = arithmetic_mutation(
                mutated
            )

        elif mutation_name == "delete":

            mutated = delete_bytes(
                mutated
            )

        elif mutation_name == "insert":

            mutated = insert_bytes(
                mutated,
                max_size
            )

    return mutated


# -------------------------------------------------------------
# CPU spike
#
# Keep the custom mutation operation simple.
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

    return mutation(
        buf
    )


# -------------------------------------------------------------
# Memory spike
#
# Avoid insertion and size growth.
# Perform only a very small in-place mutation.
# -------------------------------------------------------------

def mutate_reduce_mutation_size(
    buf,
    max_size
):

    if not buf:

        return buf

    mutated = bytearray(
        buf
    )

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
# Strongest lightweight intervention.
# -------------------------------------------------------------

def mutate_strong_throttle(
    buf,
    max_size
):

    if not buf:

        return buf

    mutated = bytearray(
        buf
    )

    index = random.randrange(
        len(mutated)
    )

    mutated[index] ^= 1

    return mutated


# -------------------------------------------------------------
# Timeout adaptation
#
# Avoid structural expansion and use small deterministic-style
# mutations.
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

    return mutation(
        buf
    )


# -------------------------------------------------------------
# Crash exploitation
#
# Stay relatively close to the existing input while applying
# several local mutation choices.
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

    return mutation(
        buf
    )


# =============================================================
# AFL++ custom mutation count
# =============================================================

def fuzz_count(buf):

    mode = read_adaptation_mode()

    # DEFAULT:
    # Let normal AFL++ fuzzing continue without requesting
    # custom-mutator attempts.
    if mode == "DEFAULT":

        return 0

    # Level-1 adaptive modes:
    # One custom-mutator attempt per requested queue entry.
    #
    # EXPLORE_HEAVY gets its extra exploration inside
    # mutate_explore_heavy(), rather than requesting many
    # additional AFL++ custom-stage executions.
    return 1


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

    global LAST_MODE

    mode = read_adaptation_mode()

    if mode != LAST_MODE:

        print(
            f"[Adaptive Mutator] Mode changed: "
            f"{LAST_MODE} -> {mode}",
            flush=True
        )

        LAST_MODE = mode

    # ---------------------------------------------------------
    # Select strategy
    # ---------------------------------------------------------

    if mode == "EXPLORE_HEAVY":

        mutated_out = (
            mutate_explore_heavy(
                buf,
                max_size
            )
        )

    elif mode == "THROTTLE_POWER":

        mutated_out = (
            mutate_throttle_power(
                buf,
                max_size
            )
        )

    elif mode == "REDUCE_MUTATION_SIZE":

        mutated_out = (
            mutate_reduce_mutation_size(
                buf,
                max_size
            )
        )

    elif mode == "STRONG_THROTTLE":

        mutated_out = (
            mutate_strong_throttle(
                buf,
                max_size
            )
        )

    elif mode == "SKIP_TIMEOUTS":

        mutated_out = (
            mutate_skip_timeouts(
                buf,
                max_size
            )
        )

    elif mode == "EXPLOIT_CRASH":

        mutated_out = (
            mutate_exploit_crash(
                buf,
                max_size
            )
        )

    else:

        mutated_out = bytearray(
            buf
        )

    # ---------------------------------------------------------
    # AFL++ maximum size
    # ---------------------------------------------------------

    if len(
        mutated_out
    ) > max_size:

        mutated_out = mutated_out[
            :max_size
        ]

    return mutated_out