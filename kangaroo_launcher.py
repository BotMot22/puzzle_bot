#!/usr/bin/env python3
"""
kangaroo_launcher.py -- Pollard's Kangaroo ECDLP Solver Launcher
================================================================

Once the public key for Bitcoin Puzzle #71 is known, this script:

  1. Explains Pollard's Kangaroo algorithm and why it works here
  2. Validates the public key against the target address
  3. Attempts to solve using iceland's BSGS (Baby-step Giant-step) functions
  4. Generates exact command lines for JeanLucPons/Kangaroo (GPU solver)
  5. Provides instructions for running on cloud GPU instances

Background:
-----------
Bitcoin Puzzle #71 has a private key in the range [2^70, 2^71 - 1].
That is a 71-bit key space with ~1.18e21 possible keys.

Brute force is infeasible, but Pollard's Kangaroo (also called the lambda
method) solves the Elliptic Curve Discrete Logarithm Problem (ECDLP) in
O(sqrt(n)) time where n is the range size.

  sqrt(2^71) = 2^35.5 ~ 48 billion steps

On a modern GPU (RTX 3090/4090) doing ~1.5 billion group operations/sec,
this takes roughly 30 seconds to a few minutes. Even on a CPU, it would
take hours rather than the billions of years needed for brute force.

The algorithm works by launching two types of "kangaroos":
  - TAME kangaroos: start from known points within the range
  - WILD kangaroos: start from the target public key (shifted into range)

Both hop pseudo-randomly across the elliptic curve group. When a wild
kangaroo lands on the same "distinguished point" as a tame kangaroo,
the private key can be computed from the difference in their accumulated
jump distances.

Usage:
  python kangaroo_launcher.py --pubkey <hex_pubkey>
  python kangaroo_launcher.py --pubkey-file /root/puzzle71/PUBKEY_FOUND.txt
"""

import argparse
import hashlib
import os
import re
import sys
import textwrap

# =============================================================================
# Constants for Puzzle #71
# =============================================================================

TARGET_ADDRESS = "1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU"
TARGET_H160 = "f6f5431d25bbf7b12e8add9af5e3475c44a0a5b8"

# Puzzle #71 key range
RANGE_START = 0x400000000000000000  # 2^70
RANGE_END   = 0x7ffffffffffffffffff  # 2^71 - 1
RANGE_BITS  = 71

# Hex strings for command-line tools
RANGE_START_HEX = "400000000000000000"
RANGE_END_HEX   = "7ffffffffffffffffff"

PUBKEY_FOUND_FILE = "/root/puzzle71/PUBKEY_FOUND.txt"

# Iceland secp256k1 library path
ICELAND_PATH = "/root/iceland_secp256k1"

# =============================================================================
# Helpers
# =============================================================================

def hash160(pubkey_bytes):
    """RIPEMD160(SHA256(pubkey))"""
    sha = hashlib.sha256(pubkey_bytes).digest()
    r = hashlib.new("ripemd160")
    r.update(sha)
    return r.hexdigest()


def validate_pubkey(pubkey_hex):
    """
    Validate that pubkey_hex is a valid secp256k1 public key that hashes
    to the puzzle #71 target address.
    """
    try:
        pk_bytes = bytes.fromhex(pubkey_hex)
    except ValueError:
        print(f"[ERROR] Invalid hex string: {pubkey_hex}")
        return False

    if len(pk_bytes) == 33:
        if pk_bytes[0] not in (0x02, 0x03):
            print("[ERROR] Compressed pubkey must start with 02 or 03")
            return False
        print(f"[OK] Compressed public key (33 bytes)")
    elif len(pk_bytes) == 65:
        if pk_bytes[0] != 0x04:
            print("[ERROR] Uncompressed pubkey must start with 04")
            return False
        print(f"[OK] Uncompressed public key (65 bytes)")
    else:
        print(f"[ERROR] Public key must be 33 or 65 bytes, got {len(pk_bytes)}")
        return False

    h = hash160(pk_bytes)
    if h.lower() == TARGET_H160.lower():
        print(f"[OK] Hash160 matches target: {h}")
        return True
    else:
        print(f"[FAIL] Hash160 mismatch!")
        print(f"       Expected: {TARGET_H160}")
        print(f"       Got:      {h}")
        # It might be that we need to try the other form (compressed vs uncompressed)
        if len(pk_bytes) == 65:
            # Try compressing
            x = pk_bytes[1:33]
            y_parity = pk_bytes[64] & 1
            prefix = bytes([0x02 + y_parity])
            compressed = prefix + x
            h2 = hash160(compressed)
            if h2.lower() == TARGET_H160.lower():
                print(f"[OK] Compressed form matches: {compressed.hex()}")
                print(f"     Use compressed key: {compressed.hex()}")
                return True
        return False


def read_pubkey_from_file(filepath):
    """Read a public key from the PUBKEY_FOUND.txt file."""
    try:
        with open(filepath, "r") as f:
            content = f.read()
        # Look for "Public Key: <hex>" line
        match = re.search(r"Public Key:\s*([0-9a-fA-F]+)", content)
        if match:
            return match.group(1)
        # Fallback: look for any 66 or 130 char hex string
        match = re.search(r"\b([0-9a-fA-F]{66})\b", content)
        if match:
            return match.group(1)
        match = re.search(r"\b([0-9a-fA-F]{130})\b", content)
        if match:
            return match.group(1)
        print(f"[ERROR] Could not find public key in {filepath}")
        return None
    except FileNotFoundError:
        print(f"[ERROR] File not found: {filepath}")
        return None


def get_compressed_pubkey(pubkey_hex):
    """Ensure pubkey is in compressed form (33 bytes)."""
    pk_bytes = bytes.fromhex(pubkey_hex)
    if len(pk_bytes) == 33:
        return pubkey_hex
    elif len(pk_bytes) == 65:
        x = pk_bytes[1:33]
        y_parity = pk_bytes[64] & 1
        prefix = bytes([0x02 + y_parity])
        return (prefix + x).hex()
    return pubkey_hex

# =============================================================================
# Iceland BSGS solver
# =============================================================================

def attempt_iceland_bsgs(pubkey_hex):
    """
    Attempt to use iceland's secp256k1 BSGS functions to solve the ECDLP.

    BSGS (Baby-step Giant-step) is a meet-in-the-middle algorithm:
      - Baby steps: compute and store k*G for k = 0, 1, ..., m-1
      - Giant steps: compute Q - j*m*G for j = 0, 1, ..., m-1
      - If baby step matches giant step: key = j*m + k

    Time complexity: O(sqrt(n)), Space complexity: O(sqrt(n))
    For puzzle #71 (71 bits): sqrt(2^71) = 2^35.5 ~ 48 billion
    This requires ~48 billion entries * ~40 bytes each ~ 1.8 TB of RAM.
    That is NOT feasible for BSGS on a single machine.

    Iceland's BSGS uses bloom filters to reduce memory, but even so,
    71-bit range is very large for BSGS. Kangaroo is the better algorithm
    here because it has the same O(sqrt(n)) time but only O(1) space.

    We attempt it anyway with a smaller baby-step table in case the
    private key happens to be near the start of the range.
    """
    print("\n" + "=" * 70)
    print("ATTEMPTING ICELAND BSGS SOLVER")
    print("=" * 70)

    # Check if library is available
    if not os.path.isfile(os.path.join(ICELAND_PATH, "secp256k1.py")):
        print(f"[WARN] Iceland secp256k1 library not found at {ICELAND_PATH}")
        print("       Skipping BSGS attempt.")
        return None

    sys.path.insert(0, ICELAND_PATH)
    try:
        import secp256k1 as ice
        print("[OK] Iceland secp256k1 library loaded.")
    except Exception as e:
        print(f"[ERROR] Failed to import iceland secp256k1: {e}")
        return None

    # Convert pubkey to uncompressed point (65 bytes) as required by iceland
    pk_bytes = bytes.fromhex(pubkey_hex)
    if len(pk_bytes) == 33:
        try:
            upub = ice.pub2upub(pubkey_hex)
            print(f"[OK] Converted to uncompressed: {upub.hex()[:20]}...")
        except Exception as e:
            print(f"[ERROR] Failed to convert pubkey: {e}")
            return None
    elif len(pk_bytes) == 65:
        upub = pk_bytes
    else:
        print(f"[ERROR] Invalid pubkey length: {len(pk_bytes)}")
        return None

    # Validate the point is on the curve
    try:
        is_valid = ice.pubkey_isvalid(upub)
        print(f"[OK] Point is on curve: {is_valid}")
        if not is_valid:
            print("[ERROR] Public key is not a valid secp256k1 point!")
            return None
    except Exception as e:
        print(f"[WARN] Could not validate point: {e}")

    # BSGS attempt with limited baby-step table
    # Using 100 million entries ~ covers 1e8 keys from start of range
    # This only works if the key is within 1e8 of RANGE_START, which is unlikely
    # but costs only a few GB of RAM and a few seconds.
    BABY_STEPS = 100_000_000  # 100 million
    print(f"\n[INFO] BSGS with {BABY_STEPS:,} baby steps (covers {BABY_STEPS:,} keys from range start)")
    print(f"[INFO] This is a long shot -- only works if key is near start of range.")
    print(f"[INFO] For full 71-bit search, use Pollard's Kangaroo (see below).")

    try:
        print("[*] Preparing BSGS baby-step table (this may take a minute)...")
        ice.bsgs_2nd_check_prepare(BABY_STEPS)
        print(f"[OK] Baby-step table ready ({BABY_STEPS:,} entries)")

        print("[*] Running BSGS check from range start...")
        found, result = ice.bsgs_2nd_check(upub, RANGE_START)

        if found:
            pvk_hex = result.hex()
            pvk_int = int(pvk_hex, 16)
            print(f"\n[!!!] PRIVATE KEY FOUND BY BSGS!")
            print(f"      Hex: {pvk_hex}")
            print(f"      Int: {pvk_int}")
            # Verify
            verify_point = ice.scalar_multiplication(pvk_int)
            if verify_point == upub:
                print(f"[OK] Verified: scalar_multiplication(pvk) matches public key!")
            return pvk_hex
        else:
            print("[INFO] BSGS did not find the key in the tested sub-range.")
            print("       This is expected -- use Kangaroo for the full range.")
    except Exception as e:
        print(f"[ERROR] BSGS failed: {e}")

    return None

# =============================================================================
# Generate Kangaroo command lines
# =============================================================================

def generate_kangaroo_commands(pubkey_hex):
    """
    Generate exact command lines for JeanLucPons/Kangaroo solver.
    https://github.com/JeanLucPons/Kangaroo
    """
    cpub = get_compressed_pubkey(pubkey_hex)

    print("\n" + "=" * 70)
    print("POLLARD'S KANGAROO SOLVER -- COMMAND LINES")
    print("=" * 70)

    print("""
ALGORITHM OVERVIEW
------------------
Pollard's Kangaroo (lambda method) solves the ECDLP in O(sqrt(n)) time
with O(1) memory, making it ideal for puzzle #71's 71-bit range.

Two types of kangaroos hop pseudo-randomly on the elliptic curve:
  - TAME: starts from a known point within [2^70, 2^71-1]
  - WILD: starts from the target public key Q

When a wild kangaroo collides with a tame kangaroo at a "distinguished
point" (a point whose x-coordinate has k leading zero bits), we can
compute: private_key = tame_distance - wild_distance

Expected operations: ~2 * sqrt(2^71 - 2^70) = ~2 * 2^35 ~ 68 billion
On RTX 4090 (~2 Gkey/s): approximately 30-60 seconds
On RTX 3090 (~1.5 Gkey/s): approximately 1-2 minutes
On CPU (100 Mkey/s): approximately 10-15 minutes
""")

    # =========================================================================
    # JeanLucPons/Kangaroo
    # =========================================================================
    print("-" * 70)
    print("OPTION 1: JeanLucPons/Kangaroo (GPU, recommended)")
    print("-" * 70)
    print(f"""
GitHub: https://github.com/JeanLucPons/Kangaroo

1. Clone and build:
   git clone https://github.com/JeanLucPons/Kangaroo.git
   cd Kangaroo
   make gpu=1    # For GPU support (requires CUDA)
   # or just: make  # For CPU only

2. Create input file (puzzle71.txt):
""")

    input_file_content = f"""{RANGE_START_HEX}
{RANGE_END_HEX}
{cpub}"""

    print(f"   Contents of puzzle71.txt:")
    print(f"   ---")
    for line in input_file_content.split("\n"):
        print(f"   {line}")
    print(f"   ---")

    print(f"""
3. Run (GPU):
   ./kangaroo -gpu puzzle71.txt

4. Run (CPU, multi-threaded):
   ./kangaroo -t 0 puzzle71.txt
   # -t 0 = use all available CPU threads

5. Run (distributed across multiple machines):
   # On server:
   ./kangaroo -gpu -s server_save.work puzzle71.txt
   # Save/restore work with -w / -i flags for checkpoints

Expected runtime: 30 seconds to 2 minutes on a modern GPU.
""")

    # =========================================================================
    # Write the input file
    # =========================================================================
    input_file_path = "/root/puzzle71/puzzle71_kangaroo_input.txt"
    try:
        with open(input_file_path, "w") as f:
            f.write(input_file_content + "\n")
        print(f"[OK] Kangaroo input file written to: {input_file_path}")
    except Exception as e:
        print(f"[WARN] Could not write input file: {e}")

    # =========================================================================
    # Alternative: KeyHunt (iceland-based BSGS/Kangaroo)
    # =========================================================================
    print("\n" + "-" * 70)
    print("OPTION 2: albertobsd/keyhunt (CPU, uses iceland library)")
    print("-" * 70)
    print(f"""
GitHub: https://github.com/albertobsd/keyhunt

1. Clone and build:
   git clone https://github.com/albertobsd/keyhunt.git
   cd keyhunt
   make

2. Run with kangaroo mode:
   ./keyhunt -m bsgs -f puzzle71_pubkeys.txt -r {RANGE_START_HEX}:{RANGE_END_HEX} -t 4 -S

3. Where puzzle71_pubkeys.txt contains:
   {cpub}
""")

    # =========================================================================
    # Quick-start one-liner
    # =========================================================================
    print("\n" + "-" * 70)
    print("QUICK REFERENCE -- One-line commands")
    print("-" * 70)
    print(f"""
# JeanLucPons Kangaroo (GPU):
cd /tmp && git clone https://github.com/JeanLucPons/Kangaroo.git && cd Kangaroo && make gpu=1 && echo -e "{RANGE_START_HEX}\\n{RANGE_END_HEX}\\n{cpub}" > in.txt && ./kangaroo -gpu in.txt

# JeanLucPons Kangaroo (CPU only):
cd /tmp && git clone https://github.com/JeanLucPons/Kangaroo.git && cd Kangaroo && make && echo -e "{RANGE_START_HEX}\\n{RANGE_END_HEX}\\n{cpub}" > in.txt && ./kangaroo -t 0 in.txt
""")

    return cpub

# =============================================================================
# Summary and explanation
# =============================================================================

def print_explanation():
    """Print detailed explanation of the approach."""
    print("""
================================================================================
BITCOIN PUZZLE #71 -- KANGAROO ATTACK STRATEGY
================================================================================

WHAT IS BITCOIN PUZZLE #71?
  A Bitcoin address whose private key is known to lie in the range
  [2^70, 2^71 - 1]. The address holds a bounty that can be claimed by
  whoever finds the private key.

  Address: 1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU
  Range:   0x400000000000000000 to 0x7ffffffffffffffffff

WHY DO WE NEED THE PUBLIC KEY?
  Bitcoin addresses are derived from public keys via hash functions:
    private_key -> public_key (EC multiplication) -> hash160 -> address

  Knowing only the address (hash of public key), we cannot run Kangaroo
  because the algorithm requires the actual elliptic curve point (public key).

  However, when an address SPENDS coins, the public key is revealed in
  the transaction's scriptSig (for P2PKH) or witness data (for SegWit).

ONCE WE HAVE THE PUBLIC KEY:
  The Elliptic Curve Discrete Logarithm Problem (ECDLP) is:
    Given Q = k*G, find k (where G is the secp256k1 generator point)

  Pollard's Kangaroo solves this in O(sqrt(range_size)) operations when
  the range of k is known. For 71-bit range:
    sqrt(2^71) ~ 2^35.5 ~ 48 billion operations

  A modern GPU can do ~1-2 billion EC operations per second, so the
  entire search takes about 30 seconds to 2 minutes.

THE RACE:
  If someone sends coins FROM the puzzle address, EVERYONE monitoring the
  blockchain can see the public key at the same time. The first person to:
    1. Extract the public key from the mempool (unconfirmed tx)
    2. Run Kangaroo to recover the private key
    3. Broadcast a competing transaction sweeping the remaining funds
  ...wins the remaining bounty. Speed is everything.
================================================================================
""")

# =============================================================================
# Entry point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Pollard's Kangaroo ECDLP Solver Launcher for Bitcoin Puzzle #71",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(f"""
            Target address: {TARGET_ADDRESS}
            Key range: [{RANGE_START_HEX}, {RANGE_END_HEX}] (71 bits)

            Supply the public key via --pubkey or --pubkey-file to generate
            solver commands.
        """),
    )
    parser.add_argument(
        "--pubkey", type=str, default=None,
        help="The public key in hex (compressed 66 chars or uncompressed 130 chars)."
    )
    parser.add_argument(
        "--pubkey-file", type=str, default=None,
        help=f"Read public key from file (default: {PUBKEY_FOUND_FILE})."
    )
    parser.add_argument(
        "--explain", action="store_true",
        help="Print detailed explanation of the attack strategy."
    )
    parser.add_argument(
        "--skip-bsgs", action="store_true",
        help="Skip the iceland BSGS attempt (go straight to Kangaroo commands)."
    )

    args = parser.parse_args()

    print("=" * 70)
    print("  BITCOIN PUZZLE #71 -- KANGAROO LAUNCHER")
    print("=" * 70)
    print(f"  Target: {TARGET_ADDRESS}")
    print(f"  Range:  {RANGE_START_HEX} .. {RANGE_END_HEX} ({RANGE_BITS} bits)")
    print("=" * 70)

    if args.explain:
        print_explanation()

    # Get the public key
    pubkey_hex = None

    if args.pubkey:
        pubkey_hex = args.pubkey.strip().lower()
    elif args.pubkey_file:
        pubkey_hex = read_pubkey_from_file(args.pubkey_file)
    elif os.path.isfile(PUBKEY_FOUND_FILE):
        print(f"\n[INFO] Found {PUBKEY_FOUND_FILE}, reading public key...")
        pubkey_hex = read_pubkey_from_file(PUBKEY_FOUND_FILE)

    if not pubkey_hex:
        print("\n[!] No public key provided or found.")
        print(f"    The public key has not been revealed on-chain yet.")
        print(f"    Run pubkey_monitor.py to watch for it.")
        print(f"\n    Usage:")
        print(f"      python kangaroo_launcher.py --pubkey <hex>")
        print(f"      python kangaroo_launcher.py --pubkey-file {PUBKEY_FOUND_FILE}")
        print(f"\n    For explanation of the strategy:")
        print(f"      python kangaroo_launcher.py --explain")
        print_explanation()
        return

    print(f"\n[*] Public key: {pubkey_hex}")

    # Validate
    print("\n[*] Validating public key...")
    if not validate_pubkey(pubkey_hex):
        print("[ERROR] Public key validation failed. Double-check the key.")
        print("        Proceeding anyway in case of alternate derivation...")

    # Attempt iceland BSGS (unless skipped)
    if not args.skip_bsgs:
        result = attempt_iceland_bsgs(pubkey_hex)
        if result:
            print(f"\n{'='*70}")
            print(f"PUZZLE SOLVED! Private key: {result}")
            print(f"{'='*70}")
            # Save result
            with open("/root/puzzle71/PRIVATE_KEY_FOUND.txt", "w") as f:
                f.write(f"Private Key (hex): {result}\n")
                f.write(f"Private Key (int): {int(result, 16)}\n")
                f.write(f"Public Key:        {pubkey_hex}\n")
                f.write(f"Address:           {TARGET_ADDRESS}\n")
            print(f"Saved to /root/puzzle71/PRIVATE_KEY_FOUND.txt")
            return

    # Generate Kangaroo solver commands
    generate_kangaroo_commands(pubkey_hex)

    print("\n" + "=" * 70)
    print("NEXT STEPS")
    print("=" * 70)
    print(f"""
1. The fastest approach is JeanLucPons/Kangaroo on a GPU.
   An RTX 4090 can solve 71-bit ECDLP in under 60 seconds.

2. The input file has been saved to:
   /root/puzzle71/puzzle71_kangaroo_input.txt

3. If you have a GPU available, build and run immediately.
   If not, rent one from vast.ai or similar ($0.20-0.50/hr).

4. TIME IS CRITICAL -- if the public key is exposed, others
   will be racing to solve it too.
""")


if __name__ == "__main__":
    main()
