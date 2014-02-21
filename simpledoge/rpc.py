import requests
import yaml
import os
import six
import logging
import sys
import argparse

from flask import current_app
from urlparse import urljoin
from itsdangerous import TimedSerializer, BadData
from bitcoinrpc.proxy import JSONRPCException

from .models import CoinTransaction
from . import create_app, coinserv


logger = logging.getLogger("toroidal")
ch = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter('[%(levelname)s] %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)


class RPCException(Exception):
    pass


class RPCClient(object):
    def __init__(self, config_path='/config.yml', root_suffix='/../',
                 max_age=5):
        self.root = os.path.abspath(os.path.dirname(__file__) + root_suffix)
        self.config = current_app.config

        self.serializer = TimedSerializer(self.config['rpc_signature'])
        self.max_age = max_age

    def post(self, url, *args, **kwargs):
        if 'data' not in kwargs:
            kwargs['data'] = ''
        kwargs['data'] = self.serializer.dumps(kwargs['data'])
        return self.remote(url, 'post', *args, **kwargs)

    def get(self, url, *args, **kwargs):
        return self.remote(url, 'get', *args, **kwargs)

    def remote(self, url, method, max_age=None, **kwargs):
        url = urljoin(self.config['url'], url)
        ret = getattr(requests, method)(url, **kwargs)
        if ret.status_code != 200:
            raise RPCException("Non 200 from remote")

        try:
            return self.serializer.loads(ret.text, max_age or self.max_age)
        except BadData:
            raise RPCException("Invalid signature: {}".format(ret.text))

    def poke_rpc(self):
        try:
            coinserv.getinfo()
        except JSONRPCException:
            raise RPCException("Coinserver not awake")

    def proc_trans(self):
        self.poke_rpc()

        transactions = self.post('get_transactions')
        txids = [t['id'] for t in transactions]
        try:
            coin_txid = CoinTransaction.from_serial_transaction(transactions)
        except Exception:
            logger.warn("Error creating transactions server side", exc_info=True)
            if self.post('confirm_transactions', data={'action': 'reset',
                                                       'txids': txids}):
                logger.info("Recieved success response from the server.")
            else:
                logger.error("Server returned failure response")
        else:
            data = {'action': 'confirm', 'coin_txid': coin_txid, 'txids': txids}
            logger.debug("Sending data back to confirm_transactions: " + str(data))
            if self.post('confirm_transactions', data=data):
                logger.info("Recieved success response from the server.")
            else:
                logger.error("Server returned failure response")


def entry():
    parser = argparse.ArgumentParser(prog='toro')
    parser.add_argument('-l',
                        '--log-level',
                        choices=['DEBUG', 'INFO', 'WARN', 'ERROR'],
                        default='WARN')
    subparsers = parser.add_subparsers(title='main subcommands', dest='action')

    subparsers.add_parser('proc_trans',
                          help='processes transactions locally by fetching '
                               'from a remote server')
    args = parser.parse_args()

    ch.setLevel(getattr(logging, args.log_level))
    logger.setLevel(getattr(logging, args.log_level))

    global_args = ['log_level', 'action']
    # subcommand functions shouldn't recieve arguments directed at the
    # global object/ configs
    kwargs = {k: v for k, v in six.iteritems(vars(args)) if k not in global_args}

    app = create_app()
    with app.app_context():
        interface = RPCClient()
        try:
            getattr(interface, args.action)(**kwargs)
        except requests.exceptions.ConnectionError:
            logger.error("Couldn't connect to remote server")
        except JSONRPCException as e:
            logger.error("Recieved exception from rpc server: {}"
                         .format(getattr(e, 'error')))
