#!/usr/bin/env python3
"""
pubkey_monitor.py -- Bitcoin Puzzle #71 Public Key Monitor
==========================================================

Monitors the Bitcoin blockchain for any OUTGOING transaction from the
puzzle #71 target address. When a P2PKH address spends coins, the public
key is revealed in the scriptSig of the spending transaction input.

Once we have the public key, Pollard's Kangaroo algorithm can solve the
discrete logarithm in the 71-bit keyspace in hours on a single GPU.

Target address : 1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU
Target h160    : f6f5431d25bbf7b12e8add9af5e3475c44a0a5b8
Puzzle #71 key range: 2^70 to 2^71 - 1
  (0x400000000000000000 to 0x7fffffffffffffffffff)

APIs polled (free, no key needed):
  - blockchain.info
  - blockchair.com
  - blockstream.info (mempool.space backend)
  - mempool.space

Usage:
  python pubkey_monitor.py [--interval 60] [--webhook URL]

Runs in a loop until stopped (Ctrl+C) or until the public key is found.
"""

import argparse
import datetime
import hashlib
import json
import os
import sys
import time
import traceback

import requests

# =============================================================================
# Constants
# =============================================================================

TARGET_ADDRESS = "1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU"
TARGET_H160 = "f6f5431d25bbf7b12e8add9af5e3475c44a0a5b8"

PUBKEY_FOUND_FILE = "/root/puzzle71/PUBKEY_FOUND.txt"
LOG_FILE = "/root/puzzle71/logs/pubkey_monitor.log"

# HTTP session with retries
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "PubkeyMonitor/1.0",
    "Accept": "application/json",
})
# Timeout for all requests (connect, read) in seconds
REQUEST_TIMEOUT = 20

# =============================================================================
# Logging
# =============================================================================

def ensure_log_dir():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

def log(msg, level="INFO"):
    """Print and write to log file with timestamp."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass  # don't crash if log write fails

# =============================================================================
# Public key extraction helpers
# =============================================================================

def hash160(pubkey_bytes):
    """RIPEMD160(SHA256(pubkey))"""
    sha = hashlib.sha256(pubkey_bytes).digest()
    r = hashlib.new("ripemd160")
    r.update(sha)
    return r.hexdigest()

def validate_pubkey_for_address(pubkey_hex):
    """
    Verify that the extracted public key hashes to the target h160.
    Returns True if the pubkey corresponds to TARGET_ADDRESS.
    """
    try:
        pubkey_bytes = bytes.fromhex(pubkey_hex)
        h = hash160(pubkey_bytes)
        return h.lower() == TARGET_H160.lower()
    except Exception:
        return False

def extract_pubkey_from_scriptsig(scriptsig_hex):
    """
    Parse a P2PKH scriptSig to extract the public key.

    Standard P2PKH scriptSig format:
      <sig_len> <signature> <pubkey_len> <pubkey>

    The public key is either 33 bytes (compressed, starts with 02/03) or
    65 bytes (uncompressed, starts with 04).

    Returns the pubkey hex string or None.
    """
    if not scriptsig_hex:
        return None
    try:
        script = bytes.fromhex(scriptsig_hex)
        idx = 0
        # Skip the signature push
        if idx >= len(script):
            return None
        sig_len = script[idx]
        idx += 1 + sig_len
        # Read the pubkey push
        if idx >= len(script):
            return None
        pk_len = script[idx]
        idx += 1
        if pk_len in (33, 65) and idx + pk_len <= len(script):
            pubkey = script[idx:idx + pk_len].hex()
            return pubkey
    except Exception:
        pass
    return None

def extract_pubkey_from_witness(witness_list):
    """
    For P2WPKH or P2SH-P2WPKH inputs, the witness contains [signature, pubkey].
    The public key is the last element (33 or 65 bytes).

    Returns the pubkey hex string or None.
    """
    if not witness_list or not isinstance(witness_list, list):
        return None
    if len(witness_list) < 2:
        return None
    try:
        last = witness_list[-1]
        # Some APIs return hex strings, others may differ
        if isinstance(last, str):
            pk_bytes = bytes.fromhex(last)
        else:
            return None
        if len(pk_bytes) in (33, 65):
            return last
    except Exception:
        pass
    return None

# =============================================================================
# API providers -- each returns (has_spending_tx, pubkey_hex_or_None, api_name)
# =============================================================================

def check_blockchain_info():
    """
    blockchain.info -- check for outgoing transactions.
    GET https://blockchain.info/rawaddr/<address>
    Look at txs where our address appears as an input.
    """
    api_name = "blockchain.info"
    url = f"https://blockchain.info/rawaddr/{TARGET_ADDRESS}?limit=50"
    try:
        resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            log(f"{api_name}: rate limited (429)", "WARN")
            return False, None, api_name
        resp.raise_for_status()
        data = resp.json()

        # n_tx = 0 means no transactions at all
        if data.get("n_tx", 0) == 0 and data.get("total_sent", 0) == 0:
            return False, None, api_name

        # If total_sent > 0 there has been spending
        if data.get("total_sent", 0) > 0:
            log(f"{api_name}: SPENDING DETECTED! total_sent={data['total_sent']}", "ALERT")
            # Scan transactions for our address as input
            for tx in data.get("txs", []):
                for inp in tx.get("inputs", []):
                    prev_out = inp.get("prev_out", {})
                    if prev_out.get("addr") == TARGET_ADDRESS:
                        script_hex = inp.get("script", "")
                        pubkey = extract_pubkey_from_scriptsig(script_hex)
                        if pubkey and validate_pubkey_for_address(pubkey):
                            return True, pubkey, api_name
                        # Also try witness if present
                        witness = inp.get("witness", None)
                        if witness:
                            pubkey = extract_pubkey_from_witness(witness)
                            if pubkey and validate_pubkey_for_address(pubkey):
                                return True, pubkey, api_name
            # Spending detected but couldn't extract key (shouldn't happen)
            return True, None, api_name

        return False, None, api_name

    except requests.exceptions.RequestException as e:
        log(f"{api_name}: request error -- {e}", "WARN")
        return False, None, api_name
    except Exception as e:
        log(f"{api_name}: parse error -- {e}", "WARN")
        return False, None, api_name


def check_blockstream():
    """
    blockstream.info -- check for outgoing transactions via its Esplora API.
    GET https://blockstream.info/api/address/<address>/txs
    """
    api_name = "blockstream.info"
    url = f"https://blockstream.info/api/address/{TARGET_ADDRESS}/txs"
    try:
        resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            log(f"{api_name}: rate limited (429)", "WARN")
            return False, None, api_name
        resp.raise_for_status()
        txs = resp.json()

        if not txs:
            return False, None, api_name

        for tx in txs:
            for vin in tx.get("vin", []):
                prevout = vin.get("prevout", {})
                scriptpubkey_addr = prevout.get("scriptpubkey_address", "")
                if scriptpubkey_addr == TARGET_ADDRESS:
                    log(f"{api_name}: SPENDING TX FOUND! txid={tx.get('txid', '?')}", "ALERT")
                    # Try scriptsig
                    scriptsig_hex = vin.get("scriptsig", "")
                    pubkey = extract_pubkey_from_scriptsig(scriptsig_hex)
                    if pubkey and validate_pubkey_for_address(pubkey):
                        return True, pubkey, api_name
                    # Try witness
                    witness = vin.get("witness", [])
                    pubkey = extract_pubkey_from_witness(witness)
                    if pubkey and validate_pubkey_for_address(pubkey):
                        return True, pubkey, api_name
                    return True, None, api_name

        return False, None, api_name

    except requests.exceptions.RequestException as e:
        log(f"{api_name}: request error -- {e}", "WARN")
        return False, None, api_name
    except Exception as e:
        log(f"{api_name}: parse error -- {e}", "WARN")
        return False, None, api_name


def check_mempool_space():
    """
    mempool.space -- check confirmed + mempool transactions.
    Confirmed: GET https://mempool.space/api/address/<address>/txs
    Mempool:   GET https://mempool.space/api/address/<address>/txs/mempool
    """
    api_name = "mempool.space"

    def _scan_txs(txs, label):
        for tx in txs:
            for vin in tx.get("vin", []):
                prevout = vin.get("prevout", {})
                scriptpubkey_addr = prevout.get("scriptpubkey_address", "")
                if scriptpubkey_addr == TARGET_ADDRESS:
                    log(f"{api_name}: SPENDING TX ({label})! txid={tx.get('txid', '?')}", "ALERT")
                    scriptsig_hex = vin.get("scriptsig", "")
                    pubkey = extract_pubkey_from_scriptsig(scriptsig_hex)
                    if pubkey and validate_pubkey_for_address(pubkey):
                        return True, pubkey
                    witness = vin.get("witness", [])
                    pubkey = extract_pubkey_from_witness(witness)
                    if pubkey and validate_pubkey_for_address(pubkey):
                        return True, pubkey
                    return True, None
        return False, None

    try:
        # Check mempool (unconfirmed) FIRST -- speed matters
        url_mempool = f"https://mempool.space/api/address/{TARGET_ADDRESS}/txs/mempool"
        resp = SESSION.get(url_mempool, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            txs = resp.json()
            if txs:
                found, pubkey = _scan_txs(txs, "mempool")
                if found:
                    return True, pubkey, api_name

        # Check confirmed transactions
        url_conf = f"https://mempool.space/api/address/{TARGET_ADDRESS}/txs"
        resp = SESSION.get(url_conf, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            log(f"{api_name}: rate limited (429)", "WARN")
            return False, None, api_name
        resp.raise_for_status()
        txs = resp.json()
        if txs:
            found, pubkey = _scan_txs(txs, "confirmed")
            if found:
                return True, pubkey, api_name

        return False, None, api_name

    except requests.exceptions.RequestException as e:
        log(f"{api_name}: request error -- {e}", "WARN")
        return False, None, api_name
    except Exception as e:
        log(f"{api_name}: parse error -- {e}", "WARN")
        return False, None, api_name


def check_blockchair():
    """
    blockchair.com -- check address stats for spent output count.
    GET https://api.blockchair.com/bitcoin/dashboards/address/<address>

    If spent outputs > 0, there is a spending tx. Then fetch raw tx to
    extract the public key.
    """
    api_name = "blockchair.com"
    url = f"https://api.blockchair.com/bitcoin/dashboards/address/{TARGET_ADDRESS}"
    try:
        resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            log(f"{api_name}: rate limited (429)", "WARN")
            return False, None, api_name
        resp.raise_for_status()
        data = resp.json()

        addr_data = data.get("data", {}).get(TARGET_ADDRESS, {})
        address_info = addr_data.get("address", {})
        spent_count = address_info.get("spent_output_count", 0)

        if spent_count > 0:
            log(f"{api_name}: SPENDING DETECTED! spent_output_count={spent_count}", "ALERT")
            # Get the spending transactions
            tx_list = addr_data.get("transactions", [])
            for txid in tx_list[:10]:  # check first 10
                pubkey = _blockchair_fetch_tx_pubkey(txid)
                if pubkey:
                    return True, pubkey, api_name
            return True, None, api_name

        # Also check mempool transactions
        mempool_txs = addr_data.get("mempool_transactions", [])
        if mempool_txs:
            log(f"{api_name}: MEMPOOL TX DETECTED! count={len(mempool_txs)}", "ALERT")
            for txid in mempool_txs[:5]:
                pubkey = _blockchair_fetch_tx_pubkey(txid)
                if pubkey:
                    return True, pubkey, api_name
            return True, None, api_name

        return False, None, api_name

    except requests.exceptions.RequestException as e:
        log(f"{api_name}: request error -- {e}", "WARN")
        return False, None, api_name
    except Exception as e:
        log(f"{api_name}: parse error -- {e}", "WARN")
        return False, None, api_name


def _blockchair_fetch_tx_pubkey(txid):
    """
    Fetch a raw transaction from blockchair and extract pubkey from inputs
    that spend from our target address.
    Uses blockstream as a fallback since blockchair raw tx API is limited.
    """
    try:
        url = f"https://blockstream.info/api/tx/{txid}"
        resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        tx = resp.json()
        for vin in tx.get("vin", []):
            prevout = vin.get("prevout", {})
            if prevout.get("scriptpubkey_address", "") == TARGET_ADDRESS:
                scriptsig_hex = vin.get("scriptsig", "")
                pubkey = extract_pubkey_from_scriptsig(scriptsig_hex)
                if pubkey and validate_pubkey_for_address(pubkey):
                    return pubkey
                witness = vin.get("witness", [])
                pubkey = extract_pubkey_from_witness(witness)
                if pubkey and validate_pubkey_for_address(pubkey):
                    return pubkey
    except Exception:
        pass
    return None

# =============================================================================
# API rotation
# =============================================================================

# All available checkers
API_CHECKERS = [
    check_mempool_space,    # checks mempool first -- highest priority
    check_blockstream,
    check_blockchain_info,
    check_blockchair,
]

# =============================================================================
# Alert on pubkey found
# =============================================================================

def save_pubkey(pubkey_hex, api_name):
    """Save the discovered public key to file."""
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    content = (
        f"BITCOIN PUZZLE #71 -- PUBLIC KEY FOUND\n"
        f"======================================\n"
        f"Timestamp : {ts}\n"
        f"Address   : {TARGET_ADDRESS}\n"
        f"H160      : {TARGET_H160}\n"
        f"Public Key: {pubkey_hex}\n"
        f"Source API: {api_name}\n"
        f"======================================\n"
        f"\n"
        f"Next step: Run kangaroo_launcher.py with this public key to solve puzzle #71.\n"
        f"\n"
        f"  python /root/puzzle71/kangaroo_launcher.py --pubkey {pubkey_hex}\n"
    )
    with open(PUBKEY_FOUND_FILE, "w") as f:
        f.write(content)
    log(f"Public key saved to {PUBKEY_FOUND_FILE}")


def print_massive_alert(pubkey_hex, api_name):
    """Print a highly visible alert."""
    alert = f"""
################################################################################
################################################################################
##                                                                            ##
##   ######  ##   ## ######  ##  ## ######## ##    ##    ######## ####### ##   ##
##   ##   ## ##   ## ##   ## ## ##  ##        ##  ##     ##       ##   ## ##   ##
##   ######  ##   ## ######  ####   ######    ####      ######   ##   ## ##   ##
##   ##      ##   ## ##   ## ## ##  ##         ##       ##       ##   ## ##   ##
##   ##       #####  ######  ##  ## ########   ##       ##       ####### #### ##
##                                                                            ##
##   PUBLIC KEY FOR PUZZLE #71 HAS BEEN FOUND ON-CHAIN!                       ##
##                                                                            ##
##   Address  : {TARGET_ADDRESS}                          ##
##   PubKey   : {pubkey_hex[:64]}  ##
##              {pubkey_hex[64:]:64s}  ##
##   Source   : {api_name:53s}  ##
##                                                                            ##
##   SAVED TO : {PUBKEY_FOUND_FILE:53s}  ##
##                                                                            ##
##   RUN KANGAROO SOLVER NOW!                                                 ##
##   python /root/puzzle71/kangaroo_launcher.py --pubkey <key>                ##
##                                                                            ##
################################################################################
################################################################################
"""
    print(alert, flush=True)
    log(f"PUBLIC KEY FOUND: {pubkey_hex} (via {api_name})", "CRITICAL")


def trigger_webhook(webhook_url, pubkey_hex, api_name):
    """POST the pubkey discovery to a webhook URL."""
    if not webhook_url:
        return
    try:
        payload = {
            "event": "pubkey_found",
            "address": TARGET_ADDRESS,
            "pubkey": pubkey_hex,
            "source_api": api_name,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "message": f"Bitcoin Puzzle #71 public key found! {pubkey_hex}",
        }
        resp = SESSION.post(webhook_url, json=payload, timeout=10)
        log(f"Webhook triggered: {resp.status_code}")
    except Exception as e:
        log(f"Webhook failed: {e}", "WARN")

# =============================================================================
# Main monitor loop
# =============================================================================

def run_monitor(interval, webhook_url):
    """
    Main loop: rotate through API providers, check for spending transactions.
    """
    ensure_log_dir()

    log("=" * 70)
    log("Bitcoin Puzzle #71 -- Public Key Monitor STARTED")
    log(f"Target address : {TARGET_ADDRESS}")
    log(f"Target h160    : {TARGET_H160}")
    log(f"Check interval : {interval}s")
    log(f"Webhook        : {webhook_url or 'not configured'}")
    log(f"Pubkey file    : {PUBKEY_FOUND_FILE}")
    log(f"Log file       : {LOG_FILE}")
    log(f"APIs           : {', '.join(fn.__name__ for fn in API_CHECKERS)}")
    log("=" * 70)

    # Track which API to use next (round-robin)
    api_idx = 0
    check_count = 0
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 20  # after this many, wait longer

    while True:
        try:
            check_count += 1
            checker = API_CHECKERS[api_idx % len(API_CHECKERS)]
            api_idx += 1

            log(f"Check #{check_count} via {checker.__name__}...")

            has_spending, pubkey_hex, api_name = checker()

            if has_spending:
                if pubkey_hex:
                    # *** SUCCESS -- PUBLIC KEY FOUND ***
                    print_massive_alert(pubkey_hex, api_name)
                    save_pubkey(pubkey_hex, api_name)
                    trigger_webhook(webhook_url, pubkey_hex, api_name)

                    # Cross-verify with a second API
                    log("Cross-verifying with additional APIs...")
                    for other_checker in API_CHECKERS:
                        if other_checker.__name__ == checker.__name__:
                            continue
                        try:
                            has2, pk2, api2 = other_checker()
                            if has2 and pk2:
                                log(f"Confirmed by {api2}: {pk2}")
                            elif has2:
                                log(f"Spending confirmed by {api2} (pubkey extraction pending)")
                        except Exception:
                            pass

                    log("Monitor complete. Public key has been found and saved.")
                    log(f"Run: python /root/puzzle71/kangaroo_launcher.py --pubkey {pubkey_hex}")
                    return pubkey_hex
                else:
                    # Spending detected but pubkey extraction failed
                    # Try all other APIs immediately
                    log("Spending detected but pubkey not extracted -- trying all APIs...", "WARN")
                    for other_checker in API_CHECKERS:
                        if other_checker.__name__ == checker.__name__:
                            continue
                        try:
                            has2, pk2, api2 = other_checker()
                            if pk2:
                                print_massive_alert(pk2, api2)
                                save_pubkey(pk2, api2)
                                trigger_webhook(webhook_url, pk2, api2)
                                log(f"Run: python /root/puzzle71/kangaroo_launcher.py --pubkey {pk2}")
                                return pk2
                        except Exception:
                            pass
                    log("Could not extract pubkey from any API. Will keep trying...", "WARN")
            else:
                log(f"No spending detected. ({checker.__name__})")
                consecutive_errors = 0  # reset on success

        except KeyboardInterrupt:
            log("Monitor stopped by user (Ctrl+C).")
            break
        except Exception as e:
            consecutive_errors += 1
            log(f"Unexpected error: {e}", "ERROR")
            log(traceback.format_exc(), "ERROR")
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                backoff = min(300, interval * 2)
                log(f"Too many consecutive errors ({consecutive_errors}). Backing off {backoff}s", "WARN")
                time.sleep(backoff)
                consecutive_errors = 0
                continue

        # Sleep before next check
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log("Monitor stopped by user (Ctrl+C).")
            break

# =============================================================================
# Entry point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Bitcoin Puzzle #71 Public Key Monitor -- watches for spending transactions "
                    "from the target address to extract the public key on-chain.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Target address: {TARGET_ADDRESS}
Target h160:    {TARGET_H160}

When a spending transaction is detected, the public key is extracted and
saved to {PUBKEY_FOUND_FILE}. Then use kangaroo_launcher.py to solve
the discrete log with Pollard's Kangaroo algorithm.
        """,
    )
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Seconds between checks (default: 60). Each check uses one API."
    )
    parser.add_argument(
        "--webhook", type=str, default=None,
        help="Webhook URL to POST when pubkey is found (optional)."
    )

    args = parser.parse_args()

    if args.interval < 10:
        log("Warning: interval < 10s may cause rate limiting. Setting to 10s.", "WARN")
        args.interval = 10

    run_monitor(args.interval, args.webhook)


if __name__ == "__main__":
    main()
