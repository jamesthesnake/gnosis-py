import functools
import logging
from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple

import requests
from cached_property import cached_property
from eth_abi.exceptions import InsufficientDataBytes
from eth_abi.packed import encode_abi_packed
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import BadFunctionCallOutput

from .. import EthereumClient
from ..constants import NULL_ADDRESS
from ..contracts import (get_erc20_contract, get_kyber_network_proxy_contract,
                         get_uniswap_factory_contract,
                         get_uniswap_v2_factory_contract,
                         get_uniswap_v2_pair_contract,
                         get_uniswap_v2_router_contract)

logger = logging.getLogger(__name__)


class OracleException(Exception):
    pass


class InvalidPriceFromOracle(OracleException):
    pass


class CannotGetPriceFromOracle(OracleException):
    pass


class PriceOracle(ABC):
    @abstractmethod
    def get_price(self, *args) -> float:
        pass


class KyberOracle(PriceOracle):
    # This is the `tokenAddress` they use for ETH ¯\_(ツ)_/¯
    ETH_TOKEN_ADDRESS = '0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE'

    def __init__(self, ethereum_client: EthereumClient, kyber_network_proxy_address: str):
        """
        :param ethereum_client:
        :param kyber_network_proxy_address: https://developer.kyber.network/docs/MainnetEnvGuide/#contract-addresses
        """
        self.ethereum_client = ethereum_client
        self.w3 = ethereum_client.w3
        self.kyber_network_proxy_address = kyber_network_proxy_address
        self.kyber_network_proxy_contract = get_kyber_network_proxy_contract(self.w3,
                                                                             self.kyber_network_proxy_address)

    def get_price(self, token_address_1: str, token_address_2: str = ETH_TOKEN_ADDRESS) -> float:
        try:
            # Get decimals for token, estimation will be more accurate
            decimals = self.ethereum_client.erc20.get_decimals(token_address_1)
            token_unit = int(10 ** decimals)
            expected_rate, _ = self.kyber_network_proxy_contract.functions.getExpectedRate(token_address_1,
                                                                                           token_address_2,
                                                                                           int(token_unit)).call()

            price = expected_rate / 1e18

            if price <= 0.:
                # Try again the opposite
                expected_rate, _ = self.kyber_network_proxy_contract.functions.getExpectedRate(token_address_2,
                                                                                               token_address_1,
                                                                                               int(token_unit)).call()
                price = (token_unit / expected_rate) if expected_rate else 0

            if price <= 0.:
                error_message = f'price={price} <= 0 from kyber-network-proxy={self.kyber_network_proxy_address} ' \
                                f'for token-1={token_address_1} to token-2={token_address_2}'
                logger.warning(error_message)
                raise InvalidPriceFromOracle(error_message)
            return price
        except (ValueError, BadFunctionCallOutput, InsufficientDataBytes) as e:
            error_message = f'Cannot get price from kyber-network-proxy={self.kyber_network_proxy_address} ' \
                            f'for token-1={token_address_1} to token-2={token_address_2}'
            logger.warning(error_message)
            raise CannotGetPriceFromOracle(error_message) from e


class UniswapOracle(PriceOracle):
    def __init__(self, ethereum_client: EthereumClient, uniswap_factory_address: str):
        """
        :param ethereum_client:
        :param uniswap_factory_address: https://docs.uniswap.io/frontend-integration/connect-to-uniswap#factory-contract
        """
        self.ethereum_client = ethereum_client
        self.w3 = ethereum_client.w3
        self.uniswap_factory_address = uniswap_factory_address

    @functools.lru_cache(maxsize=None)
    def get_uniswap_exchange(self, token_address: str) -> str:
        uniswap_factory = get_uniswap_factory_contract(self.w3, self.uniswap_factory_address)
        return uniswap_factory.functions.getExchange(token_address).call()

    def _get_balances_without_batching(self, uniswap_exchange_address: str, token_address: str):
        balance = self.ethereum_client.get_balance(uniswap_exchange_address)
        token_decimals = self.ethereum_client.erc20.get_decimals(token_address)
        token_balance = self.ethereum_client.erc20.get_balance(uniswap_exchange_address, token_address)
        return balance, token_decimals, token_balance

    def _get_balances_using_batching(self, uniswap_exchange_address: str, token_address: str):
        # Use batching instead
        payload_balance = {'jsonrpc': '2.0',
                           'method': 'eth_getBalance',
                           'params': [uniswap_exchange_address, 'latest'],
                           'id': 0}
        erc20 = get_erc20_contract(self.w3, token_address)
        params = {'gas': 0, 'gasPrice': 0}
        decimals_data = erc20.functions.decimals().buildTransaction(params)['data']
        token_balance_data = erc20.functions.balanceOf(uniswap_exchange_address).buildTransaction(params)['data']
        datas = [decimals_data, token_balance_data]
        payload_calls = [{'id': i + 1, 'jsonrpc': '2.0', 'method': 'eth_call',
                          'params': [{'to': token_address, 'data': data}, 'latest']}
                         for i, data in enumerate(datas)]
        payloads = [payload_balance] + payload_calls
        r = requests.post(self.ethereum_client.ethereum_node_url, json=payloads)
        if not r.ok:
            raise CannotGetPriceFromOracle(f'Error from node with url={self.ethereum_client.ethereum_node_url}')
        results = [HexBytes(result['result']) for result in r.json()]

        balance = int(results[0].hex(), 16)
        token_decimals = self.ethereum_client.w3.codec.decode_single('uint8', results[1])
        token_balance = self.ethereum_client.w3.codec.decode_single('uint256', results[2])
        return balance, token_decimals, token_balance

    def get_price(self, token_address: str) -> float:
        uniswap_exchange_address = self.get_uniswap_exchange(token_address)

        if uniswap_exchange_address == NULL_ADDRESS:
            error_message = f'Non existing uniswap exchange for token={token_address}'
            logger.warning(error_message)
            raise CannotGetPriceFromOracle(error_message)

        try:
            balance, token_decimals, token_balance = self._get_balances_using_batching(uniswap_exchange_address,
                                                                                       token_address)
            price = balance / token_balance / 10**(18 - token_decimals)
            if price <= 0.:
                error_message = f'price={price} <= 0 from uniswap-factory={uniswap_exchange_address} ' \
                                f'for token={token_address}'
                logger.warning(error_message)
                raise InvalidPriceFromOracle(error_message)
            return price
        except (ValueError, ZeroDivisionError, BadFunctionCallOutput, InsufficientDataBytes) as e:
            error_message = f'Cannot get token balance for token={token_address}'
            logger.warning(error_message)
            raise CannotGetPriceFromOracle(error_message) from e


class UniswapV2Oracle(PriceOracle):
    def __init__(self, ethereum_client: EthereumClient,
                 uniswap_router_address: str = '0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D'):
        """
        :param ethereum_client:
        :param uniswap_router_address: https://uniswap.org/docs/v2/smart-contracts/router02/
        """
        self.ethereum_client = ethereum_client
        self.w3 = ethereum_client.w3
        self.router_address: str = uniswap_router_address
        self.router = get_uniswap_v2_router_contract(ethereum_client.w3, uniswap_router_address)
        self._decimals_cache: Dict[str, int] = {}

    @cached_property
    def factory(self):
        return get_uniswap_v2_factory_contract(self.ethereum_client.w3, self.factory_address)

    @cached_property
    def factory_address(self) -> str:
        return self.router.functions.factory().call()

    @cached_property
    def weth_address(self) -> str:
        return self.router.functions.WETH().call()

    @functools.lru_cache(maxsize=None)
    def get_pair_address(self, token_address: str, token_address_2: str) -> Optional[str]:
        """
        Get uniswap pair address. `token_address` and `token_address_2` are interchangeable.
        https://uniswap.org/docs/v2/smart-contracts/factory/
        :param token_address:
        :param token_address_2:
        :return: Address of the pair for `token_address` and `token_address_2`, if it has been created, else `None`.
        """
        # Token order does not matter for getting pair, just for creating or querying PairCreated event
        pair_address = self.factory.functions.getPair(token_address, token_address_2).call()
        if pair_address == NULL_ADDRESS:
            return None
        return pair_address

    def calculate_pair_address(self, token_address: str, token_address_2: str):
        """
        Calculate pair address without querying blockchain.
        https://uniswap.org/docs/v2/smart-contract-integration/getting-pair-addresses/#docs-header
        :param token_address:
        :param token_address_2:
        :return: Checksummed address for token pair. It could be not created yet
        """
        if token_address.lower() > token_address_2.lower():
            token_address, token_address_2 = token_address_2, token_address
        salt = Web3.keccak(encode_abi_packed(['address', 'address'],
                                             [token_address, token_address_2]))
        init_code = HexBytes('0x96e8ac4277198ff8b6f785478aa9a39f403cb768dd02cbee326c3e7da348845f')
        address = Web3.keccak(
            encode_abi_packed(['bytes', 'address', 'bytes', 'bytes'],
                              [HexBytes('ff'), self.factory_address, salt, init_code]
                              ))[-20:]
        return Web3.toChecksumAddress(address)

    def get_decimals(self, token_address: str, token_address_2: str) -> Tuple[int, int]:
        if not (token_address in self._decimals_cache and token_address_2 in self._decimals_cache):
            decimals_1, decimals_2 = self.ethereum_client.batch_call(
                [
                    get_erc20_contract(self.w3, token_address).functions.decimals(),
                    get_erc20_contract(self.w3, token_address_2).functions.decimals(),
                ])
            self._decimals_cache[token_address] = decimals_1
            self._decimals_cache[token_address_2] = decimals_2
        return self._decimals_cache[token_address], self._decimals_cache[token_address_2]

    def get_reserves(self, pair_address: str) -> Tuple[int, int]:
        """
        Returns the
        Also returns the block.timestamp (mod 2**32) of the last block during which an interaction occured for the pair.
        https://uniswap.org/docs/v2/smart-contracts/pair/
        :return: Reserves of `token_address` and `token_address_2` used to price trades and distribute liquidity.
        """
        pair = get_uniswap_v2_pair_contract(self.ethereum_client.w3, pair_address)
        # Reserves return token_1 reserves, token_2 reserves and block timestamp (mod 2**32) of last interaction
        reserves_1, reserves_2, _ = pair.functions.getReserves().call()
        return reserves_1, reserves_2

    def get_price(self, token_address: str, token_address_2: Optional[str] = None) -> float:
        token_address_2 = token_address_2 if token_address_2 else self.weth_address
        pair_address = self.calculate_pair_address(token_address, token_address_2)

        # These lines only make sense when `get_pair_address` is used. `calculate_pair_address` will always return
        # an address, even it that exchange is not deployed

        # pair_address = self.get_pair_address(token_address, token_address_2)
        # if not pair_address:
        #    error_message = f'Non existing uniswap V2 exchange for token={token_address}'
        #    logger.warning(error_message)
        #    raise CannotGetPriceFromOracle(error_message)

        try:
            # Tokens are sorted, so token_1 < token_2
            reserves_1, reserves_2 = self.get_reserves(pair_address)
            decimals_1, decimals_2 = self.get_decimals(token_address, token_address_2)
            if token_address.lower() > token_address_2.lower():
                price = reserves_1 / reserves_2
            else:
                price = reserves_2 / reserves_1
            price = price / 10 ** (decimals_2 - decimals_1)
            return price
        except (ValueError, ZeroDivisionError, BadFunctionCallOutput, InsufficientDataBytes) as e:
            error_message = f'Cannot get uniswap v2 token balance for token={token_address}'
            logger.warning(error_message)
            raise CannotGetPriceFromOracle(error_message) from e
