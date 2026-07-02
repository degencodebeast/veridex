import json
import time
import asyncio
import numpy as np

from pprint import pprint
from decimal import Decimal
from datetime import datetime
from eth_account import Account

import veridex.venues._vendor.polymarket_clob.markets as markets

from veridex.venues._vendor.polymarket_clob.throttler.httpx import HTTPClient

round_to_stepsize = lambda x,n: round(x/n)*n

def _time(units="ms"):
    if units == 's':
        return int(datetime.now().timestamp())
    if units == "ms":
        return time.time_ns()  // 1000000
    if units == "ns":
        return time.time_ns()

#constants
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
AMOY = 80002 #test
POLYGON = 137 #prod
END_CURSOR = "LTE="

DIRECT_URL = "https://data-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"

direct_endpoints = {
    "get_positions":{
        "endpoint": "/positions",
        'method': 'GET',
    },
    "get_bet_value":{
        "endpoint": "/value",
        'method': 'GET',
    }
}

#https://docs.polymarket.com/
clob_endpoints = {
    #UNDOCUMENTED
    "get_balance_allowance": {
        "endpoint": "/balance-allowance",
        "method": "GET",
    },
    "get_tick_size": {
        "endpoint": "/tick-size",
        "method": "GET",
    },
    "get_neg_risk": {
        "endpoint": "/neg-risk",
        "method": "GET",
    },

    #AUTHENTICATION
    "create_api_key":{
        "endpoint": "/auth/api-key",
        "method": "POST",
    },
    "derive_api_key": {
        "endpoint": "/auth/derive-api-key",
        "method": "GET",
    },
    "access_status": {
        "endpoint": "/auth/ban-status/cert-required",
        "method": "GET",
    },

    #ORDERS
    "post_order": {
        "endpoint": "/order",
        "method": "POST",
        "aiohttp_fallback":True,
    },
    "get_order": {
        "endpoint": "/data/order/{order_id}",
        "method": "GET",
    },
    "get_orders": {
        "endpoint": "/data/orders",
        "method": "GET",
    },
    "cancel_all_orders": {
        "endpoint": "/cancel-all",
        "method": "DELETE",
    },

    #MARKETS
    "get_market": {
        "endpoint": "/markets/{condition_id}",
        "method": "GET",
    },

    #PRICES AND BOOKS
    "get_book": {
        "endpoint": "/book",
        "method": "GET",
    },
}

class Signer():

    def __init__(self,secret,chain_id):
        self.secret = secret
        self.account = Account.from_key(secret)
        self.chain_id = chain_id

    def address(self):
        return self.account.address

    def get_chain_id(self):
        return self.chain_id

    def sign(self, message_hash):
        return Account._sign_hash(message_hash, self.secret).signature.hex()

def _default_headers(method=None):
    headers = {}
    headers["User-Agent"] = "clob_client"
    headers["Accept"] = "*/*"
    headers["Connection"] = "keep-alive"
    headers["Content-Type"] = "application/json"
    if method == "GET":
        headers["Accept-Encoding"] = "gzip"
    return headers

class Polymarket():

    def __init__(self,polymarket_key,secret,chain_id=POLYGON,sig_type=2,funder=None):
        """
        Instantiates the Polymarket wrapper class.

        Args:
            polymarket_key (str): The Polymarket account key. (Top RHS, copy address)
            secret (str): The Polygon secret key (browser wallet) or Polymarket secret (email wallet - Deposit > Expand ... > Export private key).
            chain_id (int, optional): The Polygon chain ID. Defaults to 137.
            sig_type (int, optional): The signature type. Defaults to `2`. `1` if email wallet, `2` if Browser wallet.
            funder (str, optional): The funder address. Defaults to `polymarket_key`.
        """
        #https://docs.polymarket.com/#clob-client-questions
        self.polymarket_key = polymarket_key
        self.secret = secret
        self.chain_id = chain_id

        self.direct_client = HTTPClient(base_url=DIRECT_URL)
        self.clob_client = HTTPClient(base_url=CLOB_URL)

        self.signer = Signer(secret=secret,chain_id=chain_id)
        funder = polymarket_key if funder is None else funder
        self.builder = OrderBuilder(
            self.signer, sig_type=sig_type, funder=funder
        )

    async def init_client(self):
        """
        Initialize the Polymarket client.
        """
        try:
            creds = await self.create_api_key()
        except:
            creds = await self.derive_api_key()
        self.creds = {
            "api_key": creds["apiKey"],
            "api_secret": creds["secret"],
            "api_passphrase": creds["passphrase"],
        }

    async def account_balance(self,**kwargs):
        """
        Get the account balance. Includes total equity, cash, and bets.
        """
        balances = await asyncio.gather(self._get_cash_value(),self._get_bet_value())
        return {
            markets.ACCOUNT_EQUITY: sum(balances),
            "cash": Decimal(str(balances[0])),
            "bets": Decimal(str(balances[1]))
        }

    async def positions_get(self,**kwargs):
        """
        Get all open positions.
        """
        res = await self.get_positions(**kwargs)
        pos_dict = {}
        for pos in res:
            pos_dict[pos['asset']] = {
                markets.TICKER : pos['asset'],
                markets.AMOUNT : pos['size'],
                markets.ENTRY : pos['avgPrice'],
                markets.VALUE : pos['currentValue'],
                "market": pos['conditionId'],
                "outcome": pos['outcome'],
                "event_slug": pos['eventSlug'],
                "slug": pos['slug'],
                "opposite": pos['oppositeAsset']
            }
        return markets.standard_types(pos_dict)

    async def orders_get(self,**kwargs):
        """
        Get all open orders.
        """
        res = await self.get_orders(**kwargs)
        orders = {}
        for order in res['data']:
            orders[order['id']] = {
                markets.TICKER : order['asset_id'],
                markets.ORDER_ID : order['id'],
                markets.LIMIT_PRICE : order['price'],
                markets.ORDER_AMOUNT : Decimal(order['original_size']) * (Decimal('1') if order['side'] == 'BUY' else Decimal('-1')),
                markets.ORDER_FILLED_SIZE : order['size_matched'],
                markets.TIME_IN_FORCE : order['order_type'],
                markets.TIMESTAMP : order['created_at'],
                "outcome": order['outcome'],
                "market": order['market'],
            }
        return markets.standard_types(orders)

    async def l2_book_get(self,ticker,**kwargs):
        """
        Get the level 2 book.

        Args:
            ticker (str): The token ID for `yes` or `no` of relevant market.
            as_dict (bool, optional): Whether to return the book as a dictionary. Defaults to True.
        """
        res = await self.get_book(ticker)
        bids = []
        asks = []
        for bid in res['bids']: bids.append((bid['price'],bid['size']))
        for ask in res['asks']: asks.append((ask['price'],ask['size']))

        ob = LOB(depth=100,buffer_size=1)
        timestamp = int(res['timestamp'])
        bids = np.vstack(bids).astype(np.float64) if bids else np.empty((0, 2), dtype=np.float64)
        asks = np.vstack(asks).astype(np.float64) if asks else np.empty((0, 2), dtype=np.float64)
        ob.update(
            timestamp=timestamp,
            bids=bids,
            asks=asks,
            is_snapshot=True,
            is_sorted=False
        )
        return ob

    async def limit_order(self,ticker,amount,price,tif='GTC',round_price=True,tick_size=None,**kwargs):
        """
        Place a limit order.

        Args:
            ticker (str): The token ID for `yes` or `no` of relevant market. Also known as `token_id` or `asset_id`.
            amount (float): The signed amount of the token.
            price (float): The price of the limit order.
            tif (str, optional): The time in force. Defaults to 'GTC'.
            round_price (bool, optional): Whether to round the price. Defaults to True.
            tick_size (str, optional): The tick size. Defaults to None.
        """
        if amount == 0: return
        if tif == markets.TIME_IN_FORCE_GTC: tif = "GTC"
        if tif == markets.TIME_IN_FORCE_FOK: tif = "FOK"
        if tif == markets.TIME_IN_FORCE_GTD: tif = "GTD"
        if tif == "FAK" or tif == markets.TIME_IN_FORCE_IOC: tif = "FAK"  # Fill-and-Kill (IOC equivalent)

        side = "BUY" if amount > 0 else "SELL"
        size = abs(amount)
        if tick_size is None:
            tick_size = await self.get_tick_size(ticker)
            tick_size = str(tick_size['minimum_tick_size'])
        _price = round_to_stepsize(price,float(tick_size))

        if (not round_price and price != _price) or \
            not (_price >= float(tick_size) and _price <= 1 - float(tick_size)):
            raise ValueError(f"Price:{price} is invalid for tick size {tick_size}")

        neg_risk = await self.get_neg_risk(ticker)
        neg_risk = neg_risk['neg_risk']

        order_args = OrderArgs(
            token_id=ticker,
            price=_price,
            size=size,
            side=side,
        )
        order = self.builder.create_order(
            order_args,
            CreateOrderOptions(
                tick_size=tick_size,
                neg_risk=neg_risk,
            ),
        )
        return await self.post_order(order,order_type=tif)


    async def _iterate_cursor(self,func,kwargs):
        next_cursor = kwargs['next_cursor'] if 'next_cursor' in kwargs else 'MA=='
        kwargs.pop('next_cursor',None)
        res = await func(next_cursor=next_cursor,**kwargs)
        while res['next_cursor'] != END_CURSOR:
            _res = await func(next_cursor=res['next_cursor'],**kwargs)
            res['data'].extend(_res['data'])
            res['count'] += _res['count']
            res['next_cursor'] = _res['next_cursor']
        res['limit'] = res['count']
        return res

    def get_polymarket_key(self):
        return self.polymarket_key

    def get_signer_address(self):
        return self.signer.address()

    def get_contract_config(self):
        return get_contract_config(self.chain_id)

    def get_collateral_address(self):
        contract_config = get_contract_config(self.chain_id)
        if contract_config:
            return contract_config.collateral

    def get_conditional_address(self):
        contract_config = get_contract_config(self.chain_id)
        if contract_config:
            return contract_config.conditional_tokens

    def get_exchange_address(self, neg_risk=False):
        contract_config = get_contract_config(self.chain_id, neg_risk)
        if contract_config:
            return contract_config.exchange

    '''POLYMARKET ENDPOINTS'''
    async def headful_request(self,**kwargs):
        headers = kwargs['headers'] if 'headers' in kwargs else {}
        headers.update(_default_headers())
        kwargs['headers'] = headers
        return await self.clob_client.request(**kwargs)

    '''DIRECT ENDPOINTS'''
    async def get_positions(self,limit=100,**kwargs):
        endpoint = dict(direct_endpoints["get_positions"])
        endpoint['params'] = {'user':self.polymarket_key,'limit':limit}
        return await self.direct_client.request(**endpoint)

    async def get_bet_value(self,**kwargs):
        endpoint = dict(direct_endpoints["get_bet_value"])
        endpoint['params'] = {'user':self.polymarket_key}
        return await self.direct_client.request(**endpoint)

    async def _get_cash_value(self):
        cash_collateral = await self.get_collateral_allowance()
        return int(cash_collateral['balance']) /1e6

    async def _get_bet_value(self):
        bet_value = await self.get_bet_value()
        return float(bet_value[0]['value'])

    async def get_portfolio_equity(self):
        balances = await asyncio.gather(*[
            self._get_cash_value(),
            self._get_bet_value()
        ])
        return np.sum(balances)

    '''END DIRECT ENDPOINTS'''

    '''UNDOCUMENTED ENDPOINTS'''
    async def get_collateral_allowance(self,signature_type=-1):
        """
        Get the collateral allowance amount.
        """
        return await self.get_balance_allowance(asset_type="COLLATERAL",signature_type=signature_type)

    async def get_conditional_allowance(self,token_id,signature_type=-1):
        """
        Get the conditional allowance amount.
        """
        return await self.get_balance_allowance(asset_type="CONDITIONAL",token_id=token_id,signature_type=signature_type)

    async def get_balance_allowance(self,asset_type,token_id=None,signature_type=-1,**kwargs):
        '''
        The correct token allowances must be set before orders can be placed.
        The following mainnet (Polygon) allowances should be set by the funding (maker) address.
        See: https://github.com/Polymarket/py-clob-client?tab=readme-ov-file#allowances
        '''
        assert asset_type == "COLLATERAL" or asset_type == "CONDITIONAL"
        endpoint = dict(clob_endpoints["get_balance_allowance"])
        headers = create_level_2_headers(self.signer,**self.creds,**endpoint)
        if signature_type == -1:
            signature_type = self.builder.sig_type
        endpoint['params'] = {
            "asset_type": asset_type,
            "signature_type": signature_type
        }
        if asset_type == "CONDITIONAL":
            endpoint['params']['token_id'] = token_id
        return await self.headful_request(**endpoint,headers=headers)

    async def get_tick_size(self,token_id,**kwargs):
        """
        Get the tick size of the token.
        """
        endpoint = dict(clob_endpoints["get_tick_size"])
        endpoint['params'] = {"token_id":token_id}
        return await self.headful_request(**endpoint)

    async def get_neg_risk(self,token_id,**kwargs):
        """
        Get the negative risk of the token.
        """
        endpoint = dict(clob_endpoints["get_neg_risk"])
        endpoint['params'] = {"token_id":token_id}
        return await self.headful_request(**endpoint)
    '''END UNDOCUMENTED ENDPOINTS'''

    '''AUTHENTICATION ENDPOINTS'''
    async def create_api_key(self,nonce=None):
        """
        Create an API key.
        """
        headers = create_level_1_headers(self.signer, nonce)
        return await self.headful_request(**dict(clob_endpoints["create_api_key"]),headers=headers)

    async def derive_api_key(self,nonce=None):
        """
        Derive the API key.
        """
        headers = create_level_1_headers(self.signer, nonce)
        return await self.headful_request(**dict(clob_endpoints["derive_api_key"]),headers=headers)

    async def access_status(self):
        """
        Get the access status (if user has `cert_required=True`, one is required to provide proof of residence).
        """
        signer_address = self.signer.address()
        endpoint = dict(clob_endpoints["access_status"])
        endpoint['params'] = {"address":signer_address}
        headers = create_level_2_headers(self.signer,**self.creds,**endpoint)
        return await self.headful_request(**endpoint,headers=headers)
    '''END AUTHENTICATION ENDPOINTS'''

    '''ORDERS ENDPOINTS'''
    async def post_order(self,order,order_type="GTC",**kwargs):
        """
        Post an order.
        """
        endpoint = dict(clob_endpoints["post_order"])
        endpoint['json'] = order_to_json(order,self.creds['api_key'], order_type)
        headers = create_level_2_headers(self.signer,**self.creds,**endpoint)
        return await self.headful_request(**endpoint,headers=headers)

    async def get_order(self,order_id,**kwargs):
        """
        Get an order by ID.
        """
        endpoint = dict(clob_endpoints["get_order"])
        endpoint['endpoint'] = endpoint['endpoint'].format(order_id=order_id)
        headers = create_level_2_headers(self.signer,**self.creds,**endpoint)
        return await self.headful_request(**endpoint,headers=headers)

    async def get_orders(self,**kwargs):
        """
        Get all orders with iterated pagination.
        """
        return await self._iterate_cursor(self.get_orders_page,kwargs)

    async def get_orders_page(self,**kwargs):
        """
        Get all orders without iterated pagination.
        """
        endpoint = dict(clob_endpoints["get_orders"])
        endpoint['params'] = kwargs
        headers = create_level_2_headers(self.signer,**self.creds,**endpoint)
        return await self.headful_request(**endpoint,headers=headers)

    async def cancel_all_orders(self,**kwargs):
        """
        Cancel all orders.
        """
        endpoint = dict(clob_endpoints["cancel_all_orders"])
        headers = create_level_2_headers(self.signer,**self.creds,**endpoint)
        return await self.headful_request(**endpoint,headers=headers)
    '''END ORDERS ENDPOINTS'''

    '''MARKETS ENDPOINTS'''
    async def get_market(self,condition_id,**kwargs):
        """
        Get a market by condition ID.

        Args:
            condition_id (str): The condition ID.
        """
        endpoint = dict(clob_endpoints["get_market"])
        endpoint['endpoint'] = endpoint['endpoint'].format(condition_id=condition_id)
        return await self.headful_request(**endpoint)
    '''END MARKETS ENDPOINTS'''

    '''PRICES AND BOOKS ENDPOINTS'''
    async def get_book(self,token_id,**kwargs):
        """
        Get the book for a token.

        Args:
            token_id (str): The token ID.
        """
        endpoint = dict(clob_endpoints["get_book"])
        endpoint['params'] = {"token_id":token_id}
        return await self.headful_request(**endpoint)
    '''END PRICES AND BOOKS ENDPOINTS'''

import numpy as np

class LOB():

    def __init__(self,depth=100,buffer_size=100):
        self.bids = None
        self.asks = None
        self.depth = depth

    def update(self,timestamp,bids,asks,is_snapshot,**kwargs):
        """
        Update the order book with new bid and ask data.

        Args:
            timestamp (float): The current timestamp in milliseconds.
            bids (np.ndarray): The new bid array.
            asks (np.ndarray): The new ask array.
            is_snapshot (bool): Whether the update is a snapshot (true) or an incremental update (false).
        """
        if is_snapshot:
            bids = bids[np.argsort(bids[:, 0])][::-1]
            asks = asks[np.argsort(asks[:, 0])]
            bids = bids[:self.depth, :]
            asks = asks[:self.depth, :]

        self.timestamp = timestamp
        self.bids = bids
        self.asks = asks

    def get_mid(self):
        """
        Get the mid price of the current order book.

        Returns:
            float: The mid price.
        """
        best_bid = self.bids[0, 0]
        best_ask = self.asks[0, 0]
        return (best_bid + best_ask) / 2

    def get_bids(self):
        """
        Get the current bid array.
        """
        return self.bids[:self.depth,:]

    def get_asks(self):
        """
        Get the current ask array.
        """
        return self.asks[:self.depth,:]

    def get_cumulative_size(self,dir,price):
        size = 0.0
        notional = 0.0

        if dir == 1:
            for i in range(self.depth):
                if self.asks[i, 0] > price:
                    break
                size += self.asks[i, 1]
                notional += self.asks[i, 0] * self.asks[i, 1]
        elif dir == -1:
            for i in range(self.depth):
                if self.bids[i, 0] < price:
                    break
                size += self.bids[i, 1]
                notional += self.bids[i, 0] * self.bids[i, 1]

        return size, notional


'''
MIT License

Copyright (c) 2022 Polymarket

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
'''

'''dataclasses'''
from dataclasses import dataclass
from typing import Literal, Optional

@dataclass
class OrderArgs:
    token_id: str
    price: float
    size: float #units of ConditionalToken
    side: str
    fee_rate_bps: int = 0 #charged to the order maker, charged on proceeds
    nonce: int = 0 #for onchain cancellations
    expiration: int = 0 #timestamp
    taker: str = ZERO_ADDRESS #zero address is used to indicate a public order

class OrderType:
    GTC = "GTC"
    FOK = "FOK"
    GTD = "GTD"

TickSize = Literal["0.1", "0.01", "0.001", "0.0001"]

@dataclass
class CreateOrderOptions:
    tick_size: TickSize
    neg_risk: bool

@dataclass
class RoundConfig:
    price: float
    size: float
    amount: float

@dataclass
class ContractConfig:
    exchange: str
    collateral: str #ERC20 token
    conditional_tokens: str #ERC1155 conditional token contract
'''end dataclasses'''

'''config'''
def get_contract_config(chainID: int, neg_risk: bool = False) -> ContractConfig:
    """
    Get the contract configuration for the chain
    """
    CONFIG = {
        137: ContractConfig(
            exchange="0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
            collateral="0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
            conditional_tokens="0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
        ),
        80002: ContractConfig(
            exchange="0xdFE02Eb6733538f8Ea35D585af8DE5958AD99E40",
            collateral="0x9c4e1703476e875070ee25b56a58b008cfb8fa78",
            conditional_tokens="0x69308FB512518e39F9b16112fA8d994F4e2Bf8bB",
        ),
    }

    NEG_RISK_CONFIG = {
        137: ContractConfig(
            exchange="0xC5d563A36AE78145C45a50134d48A1215220f80a",
            collateral="0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
            conditional_tokens="0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
        ),
        80002: ContractConfig(
            exchange="0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
            collateral="0x9c4e1703476e875070ee25b56a58b008cfb8fa78",
            conditional_tokens="0x69308FB512518e39F9b16112fA8d994F4e2Bf8bB",
        ),
    }

    if neg_risk:
        config = NEG_RISK_CONFIG.get(chainID)
    else:
        config = CONFIG.get(chainID)
    if config is None:
        raise Exception("Invalid chainID: ${}".format(chainID))

    return config
'''end config'''

import hmac
import hashlib
import base64

def build_hmac_signature(secret, timestamp, method, endpoint, body=None):
    """
    Creates an HMAC signature by signing a payload with the secret
    """
    base64_secret = base64.urlsafe_b64decode(secret)
    message = str(timestamp) + str(method) + str(endpoint)
    if body:
        # NOTE: Necessary to replace single quotes with double quotes
        # to generate the same hmac message as go and typescript
        message += str(body).replace("'", '"')

    h = hmac.new(base64_secret, bytes(message, "utf-8"), hashlib.sha256)

    # ensure base64 encoded
    return (base64.urlsafe_b64encode(h.digest())).decode("utf-8")

from eth_utils import keccak
from py_order_utils.utils import prepend_zx
from poly_eip712_structs import make_domain, EIP712Struct, Address, String, Uint

class ClobAuth(EIP712Struct):
    address = Address()
    timestamp = String()
    nonce = Uint()
    message = String()

CLOB_DOMAIN_NAME = "ClobAuthDomain"
CLOB_VERSION = "1"
MSG_TO_SIGN = "This message attests that I control the given wallet"

def get_clob_auth_domain(chain_id: int):
    return make_domain(name=CLOB_DOMAIN_NAME, version=CLOB_VERSION, chainId=chain_id)

def sign_clob_auth_message(signer: Signer, timestamp: int, nonce: int) -> str:
    clob_auth_msg = ClobAuth(
        address=signer.address(),
        timestamp=str(timestamp),
        nonce=nonce,
        message=MSG_TO_SIGN,
    )
    chain_id = signer.get_chain_id()
    auth_struct_hash = prepend_zx(
        keccak(clob_auth_msg.signable_bytes(get_clob_auth_domain(chain_id))).hex()
    )
    return prepend_zx(signer.sign(auth_struct_hash))

POLY_ADDRESS = "POLY_ADDRESS"
POLY_SIGNATURE = "POLY_SIGNATURE"
POLY_TIMESTAMP = "POLY_TIMESTAMP"
POLY_NONCE = "POLY_NONCE"
POLY_API_KEY = "POLY_API_KEY"
POLY_PASSPHRASE = "POLY_PASSPHRASE"

def create_level_1_headers(signer,nonce=None,**kwargs):
    """
    Creates Level 1 Poly headers for a request
    """
    timestamp = _time(units='s')

    n = 0
    if nonce is not None:
        n = nonce

    signature = sign_clob_auth_message(signer, timestamp, n)
    return {
        POLY_ADDRESS: signer.address(),
        POLY_SIGNATURE: signature,
        POLY_TIMESTAMP: str(timestamp),
        POLY_NONCE: str(n),
    }

def create_level_2_headers(
    signer,
    method,
    endpoint,
    api_key,
    api_secret,
    api_passphrase,
    json=None,
    **kwargs
):
    """
    Creates Level 2 Poly headers for a request
    """
    timestamp = _time(units='s')

    hmac_sig = build_hmac_signature(
        secret=api_secret,
        timestamp=timestamp,
        method=method,
        endpoint=endpoint,
        body=json,
    )

    return {
        POLY_ADDRESS: signer.address(),
        POLY_SIGNATURE: hmac_sig,
        POLY_TIMESTAMP: str(timestamp),
        POLY_API_KEY: api_key,
        POLY_PASSPHRASE: api_passphrase,
    }


'''utilities'''
def order_to_json(order, owner, orderType) -> dict:
    return {"order": order.dict(), "owner": owner, "orderType": orderType}

'''builder'''
BUY,SELL = "BUY","SELL"
from math import floor, ceil

def round_down(x: float, sig_digits: int) -> float:
    return floor(x * (10**sig_digits)) / (10**sig_digits)

def round_normal(x: float, sig_digits: int) -> float:
    return round(x * (10**sig_digits)) / (10**sig_digits)

def round_up(x: float, sig_digits: int) -> float:
    return ceil(x * (10**sig_digits)) / (10**sig_digits)

def to_token_decimals(x: float) -> int:
    f = (10**6) * x
    if decimal_places(f) > 0:
        f = round_normal(f, 0)
    return int(f)

def decimal_places(x: float) -> int:
    return abs(Decimal(x.__str__()).as_tuple().exponent)

from py_order_utils.builders import OrderBuilder as UtilsOrderBuilder
from py_order_utils.signer import Signer as UtilsSigner
from py_order_utils.model import (
    EOA,
    OrderData,
    SignedOrder,
    BUY as UtilsBuy,
    SELL as UtilsSell,
)

ROUNDING_CONFIG: dict[TickSize, RoundConfig] = {
    "0.1": RoundConfig(price=1, size=2, amount=3),
    "0.01": RoundConfig(price=2, size=2, amount=4),
    "0.001": RoundConfig(price=3, size=2, amount=5),
    "0.0001": RoundConfig(price=4, size=2, amount=6),
}

class OrderBuilder:
    def __init__(self, signer: Signer, sig_type=None, funder=None):
        self.signer = signer
        # Signature type used sign orders, defaults to EOA type
        self.sig_type = sig_type if sig_type is not None else EOA
        # Address which holds funds to be used.
        # Used for Polymarket proxy wallets and other smart contract wallets
        # Defaults to the address of the signer
        self.funder = funder if funder is not None else self.signer.address()

    def get_order_amounts(
        self, side: str, size: float, price: float, round_config: RoundConfig
    ):
        raw_price = round_normal(price, round_config.price)

        if side == BUY:
            raw_taker_amt = round_down(size, round_config.size)

            raw_maker_amt = raw_taker_amt * raw_price
            if decimal_places(raw_maker_amt) > round_config.amount:
                raw_maker_amt = round_up(raw_maker_amt, round_config.amount + 4)
                if decimal_places(raw_maker_amt) > round_config.amount:
                    raw_maker_amt = round_down(raw_maker_amt, round_config.amount)

            maker_amount = to_token_decimals(raw_maker_amt)
            taker_amount = to_token_decimals(raw_taker_amt)

            return UtilsBuy, maker_amount, taker_amount

        elif side == SELL:
            raw_maker_amt = round_down(size, round_config.size)

            raw_taker_amt = raw_maker_amt * raw_price
            if decimal_places(raw_taker_amt) > round_config.amount:
                raw_taker_amt = round_up(raw_taker_amt, round_config.amount + 4)
                if decimal_places(raw_taker_amt) > round_config.amount:
                    raw_taker_amt = round_down(raw_taker_amt, round_config.amount)

            maker_amount = to_token_decimals(raw_maker_amt)
            taker_amount = to_token_decimals(raw_taker_amt)

            return UtilsSell, maker_amount, taker_amount
        else:
            raise ValueError(f"order_args.side must be '{BUY}' or '{SELL}'")

    def create_order(
        self, order_args: OrderArgs, options: CreateOrderOptions
    ) -> SignedOrder:
        """
        Creates and signs an order
        """
        side, maker_amount, taker_amount = self.get_order_amounts(
            order_args.side,
            order_args.size,
            order_args.price,
            ROUNDING_CONFIG[options.tick_size],
        )

        data = OrderData(
            maker=self.funder,
            taker=order_args.taker,
            tokenId=order_args.token_id,
            makerAmount=str(maker_amount),
            takerAmount=str(taker_amount),
            side=side,
            feeRateBps=str(order_args.fee_rate_bps),
            nonce=str(order_args.nonce),
            signer=self.signer.address(),
            expiration=str(order_args.expiration),
            signatureType=self.sig_type,
        )

        contract_config = get_contract_config(
            self.signer.get_chain_id(), options.neg_risk
        )

        order_builder = UtilsOrderBuilder(
            contract_config.exchange,
            self.signer.get_chain_id(),
            UtilsSigner(key=self.signer.secret),
        )

        return order_builder.build_signed_order(data)
