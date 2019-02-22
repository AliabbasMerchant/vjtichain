import json
import time
from functools import lru_cache
from multiprocessing import Pool, Process
from threading import Thread, Timer
from typing import Any, Dict, List
from datetime import datetime
import hashlib
import inspect
import requests
import waitress
from bottle import BaseTemplate, Bottle, request, response, static_file, template, error

import utils.constants as consts
from core import Block, BlockChain, SingleOutput, Transaction, TxIn, TxOut, genesis_block
from authority import Authority
from utils.logger import logger, iplogger
from utils.storage import get_block_from_db, get_wallet_from_db, read_header_list_from_db
from utils.utils import compress, decompress, dhash
from wallet import Wallet

app = Bottle()
BaseTemplate.defaults["get_url"] = app.get_url

LINE_PROFILING = False

BLOCKCHAIN = BlockChain()

PEER_LIST: List[Dict[str, Any]] = []

MY_WALLET = Wallet()

miner = Authority()


def mining_thread_task():
    while True:
        if not miner.is_mining() and not consts.NO_MINING:
            miner.start_mining(BLOCKCHAIN.mempool, BLOCKCHAIN.active_chain, MY_WALLET)
        time.sleep(consts.MINING_INTERVAL_THRESHOLD // 2)


def send_to_all_peers(url, data):
    def request_task(peers, url, data):
        for peer in peers:
            try:
                requests.post(get_peer_url(peer) + url, data=data, timeout=(5, 1))
            except Exception as e:
                logger.debug("Server: Requests: Error while sending data in process" + str(peer))

    Process(target=request_task, args=(PEER_LIST, url, data), daemon=True).start()


def start_mining_thread():
    time.sleep(5)
    Thread(target=mining_thread_task, name="Miner", daemon=True).start()


def fetch_peer_list() -> List[Dict[str, Any]]:
    try:
        r = requests.post(consts.SEED_SERVER_URL, data={"port": consts.MINER_SERVER_PORT})
        peer_list = json.loads(r.text)
        return peer_list
    except Exception as e:
        logger.error("Could not connect to DNS Seed")
        return []


def get_peer_url(peer: Dict[str, Any]) -> str:
    return "http://" + str(peer["ip"]) + ":" + str(peer["port"])


def greet_peer(peer: Dict[str, Any]) -> bool:
    try:
        url = get_peer_url(peer)
        data = {"port": consts.MINER_SERVER_PORT, "version": consts.MINER_VERSION, "blockheight": BLOCKCHAIN.active_chain.length}
        # Send a POST request to the peer
        r = requests.post(url + "/greetpeer", data=data)
        data = json.loads(r.text)
        # Update the peer data in the peer list with the new data received from the peer.
        if data.get("blockheight", None):
            peer.update(data)
        else:
            logger.debug("Main: Peer data does not have Block Height")
            return False
        return True
    except Exception as e:
        logger.debug("Main: Could not greet peer" + str(e))
    return False


def receive_block_from_peer(peer: Dict[str, Any], header_hash) -> Block:
    r = requests.post(get_peer_url(peer) + "/getblock", data={"headerhash": header_hash})
    return Block.from_json(decompress(r.text)).object()


def check_block_with_peer(peer, hhash):
    r = requests.post(get_peer_url(peer) + "/checkblock", data={"headerhash": hhash})
    result = json.loads(r.text)
    if result:
        return True
    return False


def get_block_header_hash(height):
    return dhash(BLOCKCHAIN.active_chain.header_list[height])


def sync(max_peer):
    fork_height = BLOCKCHAIN.active_chain.length
    r = requests.post(get_peer_url(max_peer) + "/getblockhashes", data={"myheight": fork_height})
    hash_list = json.loads(decompress(r.text.encode()))
    # logger.debug("Received the Following HashList from peer " + str(get_peer_url(max_peer)))
    # logger.debug(hash_list)
    for hhash in hash_list:
        block = receive_block_from_peer(max_peer, hhash)
        if not BLOCKCHAIN.add_block(block):
            logger.error("Sync: Block received is invalid, Cannot Sync")
            break
    return


# Periodically sync with all the peers
def sync_with_peers():
    try:
        logger.debug("Sync: Calling Sync")
        PEER_LIST = fetch_peer_list()
        new_peer_list = []
        for peer in PEER_LIST:
            if greet_peer(peer):
                new_peer_list.append(peer)
        PEER_LIST = new_peer_list

        if PEER_LIST:
            max_peer = max(PEER_LIST, key=lambda k: k["blockheight"])
            logger.debug(f"Sync: Syncing with {get_peer_url(max_peer)}, he seems to have height {max_peer['blockheight']}")
            sync(max_peer)
    except Exception as e:
        logger.error("Sync: Error: " + str(e))
    Timer(consts.MINING_INTERVAL_THRESHOLD * 2, sync_with_peers).start()


def check_balance(pub_key: str) -> int:
    current_balance = 0
    for x, utxo_list in BLOCKCHAIN.active_chain.utxo.utxo.items():
        tx_out = utxo_list[0]
        if tx_out.address == pub_key:
            current_balance += int(tx_out.amount)
    return int(current_balance)


def send_bounty(bounty: int, receiver_public_key: str):
    current_balance = check_balance(MY_WALLET.public_key)
    if current_balance < bounty:
        logger.debug("Insuficient balance ")
        logger.debug("Current balance : " + str(current_balance))
        logger.debug("You need " + str(current_balance - bounty) + "more money")

    else:
        transaction = Transaction(
            version=1,
            locktime=0,
            timestamp=int(time.time()),
            vin={},
            vout={0: TxOut(amount=bounty, address=receiver_public_key), 1: TxOut(amount=0, address=MY_WALLET.public_key)},
        )
        create_transaction(transaction, MY_WALLET, bounty)

        logger.debug(transaction)
        logger.info("Wallet: Attempting to Send Transaction")
        try:
            requests.post(
                "http://0.0.0.0:" + str(consts.MINER_SERVER_PORT) + "/newtransaction",
                data=compress(transaction.to_json()),
                timeout=(5, 1),
            )
        except Exception as e:
            logger.error("Wallet: Could not Send Transaction. Try Again." + str(e))
        else:
            logger.info("Wallet: Transaction Sent, Wait for it to be Mined")


def create_transaction(tx: Transaction, w: Wallet, bounty: int):
    current_amount = 0
    i = 0
    for so, utxo_list in BLOCKCHAIN.active_chain.utxo.utxo.items():
        tx_out = utxo_list[0]
        if current_amount >= bounty:
            break
        if tx_out.address == w.public_key:
            current_amount += tx_out.amount
            tx.vin[i] = TxIn(payout=SingleOutput.from_json(so), pub_key=w.public_key, sig="")
            i += 1
    tx.vout[1].amount = current_amount - bounty
    tx.sign(w)

def log_ip(request, fname):
    client_ip = request.environ.get("HTTP_X_FORWARDED_FOR") or request.environ.get("REMOTE_ADDR")
    iplogger.info(f"{client_ip} : Called function {fname}")

@app.post("/checkBalance")
def checkingbalance():
    log_ip(request, inspect.stack()[0][3])
    data = request.json
    public_key = data["public_key"]
    current_balance = check_balance(public_key)
    return str(current_balance)


@app.post("/makeTransaction")
def make_transaction():
    log_ip(request, inspect.stack()[0][3])
    data = request.json

    bounty = int(data["bounty"])
    receiver_public_key = data["receiver_public_key"]
    sender_public_key = data["sender_public_key"]

    current_balance = check_balance(sender_public_key)

    if current_balance < bounty:
        logger.debug("Insufficient Balance to make Transaction")
        return "Insufficient Balance to make Transaction, need more " + str(bounty - current_balance)
    else:
        transaction = Transaction(
            version=1,
            locktime=0,
            timestamp=int(time.time()),
            vin={},
            vout={0: TxOut(amount=bounty, address=receiver_public_key), 1: TxOut(amount=0, address=sender_public_key)},
        )

        current_amount = 0
        i = 0
        for so, utxo_list in BLOCKCHAIN.active_chain.utxo.utxo.items():
            tx_out = utxo_list[0]
            if current_amount >= bounty:
                break
            if tx_out.address == sender_public_key:
                current_amount += tx_out.amount
                transaction.vin[i] = TxIn(payout=SingleOutput.from_json(so), pub_key=sender_public_key, sig="")
                i += 1
        transaction.vout[1].amount = current_amount - bounty

        data = {}
        data["send_this"] = transaction.to_json()
        transaction.vin = {}
        data["sign_this"] = transaction.to_json()
        return json.dumps(data)


@app.post("/sendTransaction")
def send_transaction():
    log_ip(request, inspect.stack()[0][3])
    data = request.json
    transaction = Transaction.from_json(data["transaction"]).object()
    sig = data["signature"]
    transaction.add_sign(sig)

    logger.debug(transaction)
    logger.info("Wallet: Attempting to Send Transaction")
    try:
        requests.post(
            "http://0.0.0.0:" + str(consts.MINER_SERVER_PORT) + "/newtransaction",
            data=compress(transaction.to_json()),
            timeout=(5, 1),
        )
    except Exception as e:
        logger.error("Wallet: Could not Send Transaction. Try Again." + str(e))
        return "Try Again"
    else:
        logger.info("Wallet: Transaction Sent, Wait for it to be Mined")
    return "Done"


@app.post("/transactionHistory")
def transaction_history():
    log_ip(request, inspect.stack()[0][3])
    data = request.json
    public_key = data["public_key"]
    tx_hist = BLOCKCHAIN.active_chain.transaction_history.get(public_key)
    return json.dumps(tx_hist)


@app.post("/greetpeer")
def greet_peer_f():
    log_ip(request, inspect.stack()[0][3])
    try:
        peer = {}
        peer["port"] = request.forms.get("port")
        peer["ip"] = request.remote_addr
        peer["time"] = time.time()
        peer["version"] = request.forms.get("version")
        peer["blockheight"] = request.forms.get("blockheight")

        ADD_ENTRY = True
        for entry in PEER_LIST:
            ip = entry["ip"]
            port = entry["port"]
            if ip == peer["ip"] and port == peer["port"]:
                ADD_ENTRY = False
        if ADD_ENTRY:
            PEER_LIST.append(peer)
            logger.debug("Server: Greet, A new peer joined, Adding to List")
    except Exception as e:
        logger.debug("Server: Greet Error: " + str(e))
        pass

    data = {"version": consts.MINER_VERSION, "blockheight": BLOCKCHAIN.active_chain.length}
    response.content_type = "application/json"
    return json.dumps(data)


@lru_cache(maxsize=128)
def cached_get_block(headerhash: str) -> str:
    if headerhash:
        db_block = get_block_from_db(headerhash)
        if db_block:
            return compress(db_block)
        else:
            logger.error("ERROR CALLED GETBLOCK FOR NON EXISTENT BLOCK")
    return "Hash hi nahi bheja LOL"


@app.post("/getblock")
def getblock():
    log_ip(request, inspect.stack()[0][3])
    hhash = request.forms.get("headerhash")
    return cached_get_block(hhash)


@app.post("/checkblock")
def checkblock():
    log_ip(request, inspect.stack()[0][3])
    headerhash = request.forms.get("headerhash")
    if get_block_from_db(headerhash):
        return json.dumps(True)
    return json.dumps(False)


@app.post("/getblockhashes")
def send_block_hashes():
    log_ip(request, inspect.stack()[0][3])
    peer_height = int(request.forms.get("myheight"))
    hash_list = []
    for i in range(peer_height, BLOCKCHAIN.active_chain.length):
        hash_list.append(dhash(BLOCKCHAIN.active_chain.header_list[i]))
    return compress(json.dumps(hash_list)).decode()


@lru_cache(maxsize=16)
def process_new_block(request_data: bytes) -> str:
    global BLOCKCHAIN
    block_json = decompress(request_data)
    if block_json:
        try:
            block = Block.from_json(block_json).object()
            # Check if block already exists
            if get_block_from_db(dhash(block.header)):
                logger.info("Server: Received block exists, doing nothing")
                return "Block already Received Before"
            if BLOCKCHAIN.add_block(block):
                logger.info("Server: Received a New Valid Block, Adding to Chain")

                logger.debug("Server: Sending new block to peers")
                # Broadcast block to other peers
                send_to_all_peers("/newblock", request_data)

            # TODO Make new chain/ orphan set for Block that is not added
        except Exception as e:
            logger.error("Server: New Block: invalid block received " + str(e))
            return "Invalid Block Received"

        # Kill Miner
        t = Timer(1, miner.stop_mining)
        t.start()
        return "Block Received"
    logger.error("Server: Invalid Block Received")
    return "Invalid Block"


@app.post("/newblock")
def received_new_block():
    log_ip(request, inspect.stack()[0][3])
    return process_new_block(request.body.read())


@lru_cache(maxsize=16)
def process_new_transaction(request_data: bytes) -> str:
    global BLOCKCHAIN
    transaction_json = decompress(request_data)
    if transaction_json:
        try:
            tx = Transaction.from_json(transaction_json).object()
            # Add transaction to Mempool
            if tx not in BLOCKCHAIN.mempool:
                if BLOCKCHAIN.active_chain.is_transaction_valid(tx):
                    logger.debug("Valid Transaction received, Adding to Mempool")
                    BLOCKCHAIN.mempool.add(tx)
                    # Broadcast block to other peers
                    send_to_all_peers("/newtransaction", request_data)
                else:
                    return "Transaction Already received"
            else:
                logger.debug("The transation is not valid, not added to Mempool")
                return "Not Valid Transaction"
        except Exception as e:
            logger.error("Server: New Transaction: Invalid tx received: " + str(e))
            raise e
            return "Not Valid Transaction"
    return "Done"


# Transactions for all active chains
@app.post("/newtransaction")
def received_new_transaction():
    log_ip(request, inspect.stack()[0][3])
    return process_new_transaction(request.body.read())


@app.get("/")
def home():
    log_ip(request, inspect.stack()[0][3])
    data = {}
    data["Blockchain Length"] = str(BLOCKCHAIN.active_chain.length)
    data["Last Block Hash"] = dhash(BLOCKCHAIN.active_chain.header_list[-1])
    data["Public Key"] = str(get_wallet_from_db(consts.MINER_SERVER_PORT)[1])
    data["Balance"] = str(check_balance(MY_WALLET.public_key))
    return template("index.html", data=data)


@app.get("/wallet")
def wallet():
    log_ip(request, inspect.stack()[0][3])
    return template("wallet.html", message="", message_type="", pubkey=MY_WALLET.public_key)


@app.post("/wallet")
def wallet_post():
    log_ip(request, inspect.stack()[0][3])
    receiver = str(request.forms.get("port"))
    bounty = request.forms.get("amount")
    message = ""
    message_type = "info"
    try:
        publickey = ""
        if len(receiver) < 10:
            wallet = get_wallet_from_db(receiver)
            if wallet is not None:
                publickey = wallet[1]
            else:
                message = "Error with the Receiver Port ID, try again."
                message_type = "danger"
                return template("wallet.html", message=message, message_type=message_type, pubkey=MY_WALLET.public_key)
        else:
            publickey = receiver
        amt = int(bounty)
        if check_balance(MY_WALLET.public_key) > amt:
            message = "Your transaction is sent, please wait for it to be mined!"
            send_bounty(amt, publickey)
        else:
            message = "You have Insufficient Balance!"
            message_type = "warning"
        return template("wallet.html", message=message, message_type=message_type, pubkey=MY_WALLET.public_key)
    except Exception as e:
        message = "Some Error Occured. Please try again later."
        message_type = "danger"
        return template("wallet.html", message=message, message_type=message_type, pubkey=MY_WALLET.public_key)


@app.get("/checkmybalance")
def checkblance():
    log_ip(request, inspect.stack()[0][3])
    return str(check_balance(MY_WALLET.public_key))


@app.route("/static/<filename:path>", name="static")
def serve_static(filename):
    log_ip(request, inspect.stack()[0][3])
    return static_file(filename, root="static")


@app.get('/favicon.ico')
def get_favicon():
    log_ip(request, inspect.stack()[0][3])
    return static_file('favicon.ico', root='static')


@app.get("/info")
def sendinfo():
    log_ip(request, inspect.stack()[0][3])
    s = (
        "No. of Blocks: "
        + str(BLOCKCHAIN.active_chain.length)
        + "<br>"
        + dhash(BLOCKCHAIN.active_chain.header_list[-1])
        + "<br>"
        + "Balance "
        + str(check_balance(MY_WALLET.public_key))
        + "<br>Public Key: <br>"
        + str(get_wallet_from_db(consts.MINER_SERVER_PORT)[1])
    )
    return s


def render_block_header(hdr):
    html = "<table>"

    html += "<tr><th>" + "Height" + "</th>"
    html += "<td>" + str(hdr.height) + "</td></tr>"

    html += "<tr><th>" + "Block Hash" + "</th>"
    html += "<td>" + dhash(hdr) + "</td></tr>"

    html += "<tr><th>" + "Prev Block Hash" + "</th>"
    html += "<td>" + str(hdr.prev_block_hash) + "</td></tr>"

    html += "<tr><th>" + "Merkle Root" + "</th>"
    html += "<td>" + str(hdr.merkle_root) + "</td></tr>"

    html += "<tr><th>" + "Timestamp" + "</th>"
    html += (
        "<td>"
        + str(datetime.fromtimestamp(hdr.timestamp).strftime("%d-%m-%Y %H:%M:%S"))
        + " ("
        + str(hdr.timestamp)
        + ")</td></tr>"
    )

    # get block
    block = Block.from_json(get_block_from_db(dhash(hdr))).object()

    html += "<tr><th>" + "Transactions" + "</th>"
    html += "<td>" + str(len(block.transactions)) + "</td></tr>"

    # for i, transaction in enumerate(block.transactions):
    #     s = "coinbase: " + str(transaction.is_coinbase) + ", fees: " + str(transaction.fees)
    #     html += "<tr><th>Transaction " + str(i) + "</th><td>" + str(s) + "</td></tr>"

    html += "</table>"
    return str(html)


@app.get("/chains")
def visualize_chain():
    log_ip(request, inspect.stack()[0][3])
    data = []
    start = BLOCKCHAIN.active_chain.length - 10 if BLOCKCHAIN.active_chain.length > 10 else 0
    headers = []
    hdr_list = BLOCKCHAIN.active_chain.header_list
    if len(hdr_list) > 200:
        hdr_list = BLOCKCHAIN.active_chain.header_list[:100] + BLOCKCHAIN.active_chain.header_list[-100:]
    for hdr in hdr_list:
        d = {}
        d["hash"] = dhash(hdr)[-5:]
        d["time"] = hdr.timestamp
        d["data"] = render_block_header(hdr)
        headers.append(d)
    data.append(headers)
    return template("chains.html", data=data, start=start)


@app.get("/explorer")
def explorer():
    log_ip(request, inspect.stack()[0][3])
    prev = int(request.query.prev or 0)
    if prev < 0:
        prev = 0
    hdr_list = list(reversed(BLOCKCHAIN.active_chain.header_list))
    indexes = [i for i in range(prev * 5, (prev + 1) * 5) if i < len(hdr_list)]
    blocks = [Block.from_json(get_block_from_db(dhash(hdr_list[i]))).object() for i in indexes]
    transactions = list(BLOCKCHAIN.mempool)
    return template("explorer.html", blocks=blocks, transactions=transactions, prev=prev)


@app.route("/transaction/<blockhash>/<txhash>", name="transaction")
def transaction(blockhash, txhash):
    log_ip(request, inspect.stack()[0][3])
    try:
        block = Block.from_json(get_block_from_db(blockhash)).object()
        tx = None
        for t in block.transactions:
            if t.hash() == txhash:
                tx = t
    except Exception as e:
        logger.debug(e)
        return template("error.html")
    return template("transaction.html", tx=tx, block=block)


@app.route("/address/<pubkey:re:.+>", name="account")
def account(pubkey):
    log_ip(request, inspect.stack()[0][3])
    balance = check_balance(pubkey)
    tx_hist = BLOCKCHAIN.active_chain.transaction_history.get(pubkey)
    return template("account.html", tx_hist=tx_hist, balance=balance, pubkey=pubkey)


@app.post("/mining")
def mining():
    log_ip(request, inspect.stack()[0][3])
    password = request.body.read().decode("utf-8")
    logger.debug(password)
    hashed = b'\x11`\x1e\xdd\xd1\xb6\x80\x0f\xd4\xb0t\x90\x9b\xd3]\xa0\xcc\x1d\x04$\x8b\xb1\x19J\xaa!T5-\x9eJ\xfcI5\xc0\xbb\xf5\xb1\x9d\xba\xbef@\xa1)\xcf\x9b]c(R\x91\x0e\x9dMM\xb6\x94\xa9\xe2\x94il\x15'
    dk = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), b'forgeteverythingthatyouthinkyouknow', 200000)
    if hashed == dk:
        consts.NO_MINING = not consts.NO_MINING
        logger.info("Mining: " + str(not consts.NO_MINING))
        return "Mining Toggled, " + "NOT MINING" if consts.NO_MINING else "MINING"
    else:
        return "Password Mismatch"


@app.route("/<url:re:.+>")
@error(403)
@error(404)
@error(505)
def error_handle(url="url", error="404"):
    log_ip(request, inspect.stack()[0][3])
    return template("error.html")


if __name__ == "__main__":
    try:
        if consts.NEW_BLOCKCHAIN:
            logger.info("FullNode: Starting New Chain from Genesis")
            BLOCKCHAIN.add_block(genesis_block)
        else:
            # Restore Blockchain
            logger.info("FullNode: Restoring Existing Chain")
            header_list = read_header_list_from_db()
            BLOCKCHAIN.build_from_header_list(header_list)

        # Sync with all my peers
        sync_with_peers()

        # Start mining Thread
        Thread(target=start_mining_thread, daemon=True).start()
        if consts.NO_MINING:
            logger.info("FullNode: Not Mining")

        # Start server
        if LINE_PROFILING:
            from wsgi_lineprof.middleware import LineProfilerMiddleware

            with open("lineprof" + str(consts.MINER_SERVER_PORT) + ".log", "w") as f:
                app = LineProfilerMiddleware(app, stream=f, async_stream=True)
                waitress.serve(app, host="0.0.0.0", threads=16, port=consts.MINER_SERVER_PORT)
        else:
            waitress.serve(app, host="0.0.0.0", threads=16, port=consts.MINER_SERVER_PORT)

    except KeyboardInterrupt:
        miner.stop_mining()
