import json
import argparse
import asyncio
import os.path

from getpass import getpass
from aiofile import async_open

from dacite import from_dict

from miner import Miner
from core import User, Peer, Net, Transaction, Invoice, Payment, Message, Reward, Block, Blockchain, BlockCheck


class CLI:
    def __init__(self):
        self.net = None
        self.usr = None
        self.chain = None

    @staticmethod
    async def _dict_to_disk(obj, obj_path):
        async with async_open(obj_path, 'w') as f:
            obj_json = json.dumps(obj.to_dict(), indent=4)
            await f.write(obj_json)

    @staticmethod
    async def _dict_from_disk(obj_path):
        async with async_open(obj_path, 'r') as f:
            return json.loads(await f.read())

    @staticmethod
    def _init_ser_obj(obj_path, obj_reader, obj_maker):
        obj = None
        if os.path.exists(obj_path):
            obj = obj_reader(asyncio.run(CLI._dict_from_disk(obj_path)))
        else:
            obj = obj_maker()
            asyncio.run(CLI._dict_to_disk(obj, obj_path))
        return obj

    def net_init(self, peers_path):
        def maker():
            net = Net(hash=None)
            net.add_peer(Peer('2002:c257:6f39::1', 10000))
            net.add_peer(Peer('2002:c257:65d4::1', 10000))
            return net

        reader = lambda d: from_dict(Net, d)
        self.net = CLI._init_ser_obj(peers_path, reader, maker)
        self.update_self_peer()

    def usr_init(self, usr_path):
        reader = CLI.usr_login
        maker = CLI.usr_reg
        self.usr = CLI._init_ser_obj(usr_path, reader, maker)

    def chain_init(self, chain_path):
        # FIXME: fetch blockchain from another node
        reader = lambda d: from_dict(Blockchain, d)
        maker = lambda: Blockchain(ver='0.1', blocks={}, hash=None)
        self.chain = CLI._init_ser_obj(chain_path, reader, maker)

    @staticmethod
    def act_with_passwd(act):
        while True:
            try:
                passwd = getpass('Password: ')
                return act(passwd)
            except KeyboardInterrupt:
                exit()
            except Exception:
                print('Invalid password!')

    @staticmethod
    def gen_passwd():
        while True:
            try:
                passwd0 = getpass('Password: ')
                passwd1 = getpass('Repeat password:')

                if passwd0 == passwd1:
                    return passwd0

                print('Passwords mismatch, please, try again.')
            except KeyboardInterrupt:
                exit()

    def passwd(self):
        return CLI.act_with_passwd(self.usr.check_passwd)

    @staticmethod
    def usr_login(usr_dict):
        return from_dict(User, usr_dict)

    @staticmethod
    def usr_reg():
        print('No user presented, register new one.')
        return User.create(CLI.gen_passwd())

    def make_trans(self, trans):
        ans = input('Do u want to make a transaction? [y/n]: ')
        if ans in ('y', 'Y'):
            trans.sign(self.usr, self.passwd())
            asyncio.run(self.net.send({'trans': trans.to_dict()}))
            print(trans.to_dict())

    def update_self_peer(self):
        self.net.update_peer(Peer(self.net.ipv6, 10000))
        asyncio.run(self.net.send(self.net.to_dict()))
        asyncio.run(self._dict_to_disk(self.net, 'peers.json'))


class CoreServer(CLI):
    def __init__(self):
        super().__init__()

    async def update_peers_hlr(self, peers_dict):
        peers = [Peer(peer['ipv6'], peer['port']) for peer in peers_dict]

        if self.net.update_peers(peers):
            print('Peers updated.')

            await self.net.send({'peers': peers_dict})
            await self._dict_to_disk(self.net, 'peers.json')

    async def add_block_hlr(self, block_dict):
        block = from_dict(Block, block_dict)

        if self.chain.check_block(block) is BlockCheck.OK:
            await self.net.send({'block': block.to_dict()})

        if self.chain.add_block(block):
            await self._dict_to_disk(self.chain, 'blockchain.json')

    async def serve_dispatch(self, data):
        hlr_map = {
            'peers': self.update_peers_hlr,
            'block': self.add_block_hlr
        }

        for key, hlr in hlr_map.items():
            if data.get(key):
                await hlr(data[key])

    async def serve_forever(self):
        self.net.serv_init(self.serve_dispatch)

        loop = asyncio.get_running_loop()
        loop.create_task(self.net.serv)

        while True:
            await asyncio.sleep(0)


class MiningServer(CoreServer):
    def __init__(self):
        super().__init__()
        self.block = None
        self.miner = Miner()
        self.trans_cache = []

    def cache_trans(self, trans):
        print(f'Transaction {trans.dict_hash()[0:12]} will be in next block.')
        self.trans_cache.append(trans)

    def make_trans(self, trans):
        super().make_trans(trans)
        self.cache_trans(trans)

    async def update_block(self):
        # wait until block will be accepted or rejected
        while self.chain.get_block_confirms(self.block):
            await asyncio.sleep(0)

        # generate new block
        self.block = self.chain.new_block(self.usr.pub)

        # clear transactions queue
        for trans in self.trans_cache:
            self.chain.add_trans(self.block, trans)
        self.trans_cache.clear()

    def add_trans_hlr(self, trans_dict):
        trans = from_dict(Transaction, trans_dict)
        self.cache_trans(trans)

    async def serve_dispatch(self, data):
        await super().serve_dispatch(data)

        # add trans
        if data.get('trans'):
            self.add_trans_hlr(data['trans'])

    async def serve_mining(self):
        while True:
            await self.update_block()

            # mining
            self.miner.set_block(self.block)
            await self.miner.work()
            print(f'Block {self.block.dict_hash()[0:12]} solved: reward {self.chain.reward()} picocoins.')

            # check and send
            if self.chain.check_block(self.block) is BlockCheck.OK:
                reward_act = Reward(self.chain.reward(), self.block.dict_hash())
                reward_trans = Transaction(from_adr=None, to_adr=self.block.pow.solver, act=reward_act, hash=None, sign=None)
                self.cache_trans(reward_trans)

                await self.net.send({'trans': reward_trans.to_dict()})
                await self.net.send({'block': self.block.to_dict()})

            if self.chain.add_block(self.block):
                await self._dict_to_disk(self.chain, 'blockchain.json')

    async def serve_forever(self):
        loop = asyncio.get_running_loop()
        loop.create_task(self.serve_mining())

        while True:
            await super().serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(prog='python3 pico-cli.py', description='PicoCoin core cli.')
    parser.add_argument('--usr', type=str, default='user.json', help='path to user keys')
    parser.add_argument('--chain', type=str, default='blockchain.json', help='path to blockchain')
    parser.add_argument('--peers', type=str, default='peers.json', help='path to peers')
    parser.add_argument('--mining', action='store_true', help='work as mining server')
    parser.add_argument('--adr',  type=str, default='127.0.0.1', help='server listen address (default: "127.0.0.1")')
    parser.add_argument('--trans', nargs=3, metavar=('to', 'act', 'args'), help='make a transaction')
    parser.add_argument('--bal', action='store_true', help='get user balance')
    parser.add_argument('--debg', action='store_true', help='debug mode (use with \'python3 -i\' flag)')

    args = parser.parse_args()

    # init core server
    serv = CoreServer() if not args.mining else MiningServer()

    serv.usr_init(args.usr)
    serv.chain_init(args.chain)

    # get balance
    if args.bal:
        print(f'Balance: {serv.chain.get_bal(serv.usr.pub)} picocoins.')
        if not args.mining:
            exit()

    serv.net_init(args.peers)

    # make transaction
    if args.trans:
        to = args.trans[0]
        act_args = args.trans[2]

        act = {
            'ivc': lambda: Invoice(int(act_args)),
            'pay': lambda: Payment(int(act_args)),
            'msg': lambda: Message(act_args)
        }[args.trans[1]]()

        trans = Transaction(from_adr=serv.usr.pub, to_adr=to, act=act, hash=None)
        serv.make_trans(trans)

        if not args.mining:
            exit()

    # serve
    if not args.debg:
        asyncio.run(serv.serve_forever())
