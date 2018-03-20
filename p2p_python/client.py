#!/user/env python3
# -*- coding: utf-8 -*-


import copy
import logging
import os.path
import random
import threading
import time
from hashlib import sha256
from tempfile import gettempdir

import bjson

from p2p_python.tool.upnpc import UpnpClient
from .core import Core, MAX_RECEIVE_SIZE
from .encryption import EncryptRSA
from .utils import StackDict, QueueSystem, is_reachable, trim_msg, get_data_path

LOCAL_IP = UpnpClient.get_localhost_ip()
GLOBAL_IP = UpnpClient.get_global_ip()

# Constant type
T_REQUEST = 'type/client/request'
T_RESPONSE = 'type/client/response'
T_ACK = 'type/client/ack'


class ClientCmd:
    PING_PONG = 'cmd/client/ping-pong'  # ping-pong
    BROADCAST = 'cmd/client/broadcast'  # 全ノードに伝播
    GET_PEER_INFO = 'cmd/client/get-peer-info'  # 隣接ノードの情報を取得
    GET_PEERS = 'cmd/client/get-peers'  # ピアリストを取得
    CHECK_REACHABLE = 'cmd/client/check-reachable'  # 外部からServerに到達できるかチェック
    FILE_CHECK = 'cmd/client/file-check'  # Fileが存在するかHashをチェック
    FILE_GET = 'cmd/client/file-get'  # Fileの転送を依頼
    FILE_DELETE = 'cmd/client/file-delete'  # 全ノードからFileを消去
    DIRECT_CMD = 'cmd/client/direct-cmd'  # 隣接ノードに直接CMDを打つ


class PeerClient:
    f_stop = False
    f_finish = False
    number = 0

    def __init__(self, port, net_ver, listen=15, f_debug=False):
        self.broadcast_que = QueueSystem()
        self.direct_cmd_que = QueueSystem()
        self.result = StackDict()
        self.direct_cmd_result = StackDict()
        self.waiting_ack = list()
        self.file_client_path = StackDict()
        host = '127.0.0.1' if f_debug else ''
        self.p2p = Core(host=host, port=port, net_ver=net_ver, listen=listen)
        self.f_debug = f_debug
        # check existence tmp dir
        tmp_dir_name = 'p2p_python.' + str(net_ver) + '.' + str(port) + ('.test' if f_debug else '')
        self.tmp_dir = os.path.join(gettempdir(), tmp_dir_name)
        if not os.path.isdir(self.tmp_dir):
            os.makedirs(self.tmp_dir)
            logging.info("Create tmp dir.")
        # check existence data dir
        data_dir_name = 'p2p_python_' + str(net_ver) + '_' + str(port) + ('.test' if f_debug else '')
        self.data_dir = os.path.join(get_data_path(), data_dir_name)
        if not os.path.isdir(self.data_dir):
            os.makedirs(self.data_dir)
            logging.info("Create data dir")
        # input first peer data
        self.peers = self.get_peers()
        # recode traffic if f_debug true
        if f_debug:
            self.p2p.traffic.recode_dir = self.data_dir

    def get_peers(self):
        peer_path = os.path.join(self.tmp_dir, 'peer.dat')
        try:
            with open(peer_path, mode='br') as f:
                return bjson.load(fp=f)
        except (FileNotFoundError, IndexError):
            with open(peer_path, mode='bw') as f:
                bjson.dump(dict(), f)
            return dict()

    def update_peers(self, updates):
        peer_path = os.path.join(self.tmp_dir, 'peer.dat')
        with open(peer_path, mode='bw') as f:
            bjson.dump(updates, f)

    def close_client(self):
        # Stop P2P connection all
        self.p2p.close_server()
        self.f_stop = True
        for client in self.p2p.client:
            self.p2p.remove_connection(client)
        self.p2p.stream_que.put((None, None))
        while not self.f_finish:
            time.sleep(1)

    def start(self, f_server=True, f_stabilize=True):
        def processing():
            client = raw_byte_msg = None
            while not self.f_stop:
                try:
                    client, raw_byte_msg = self.p2p.stream_que.get()
                    if client is None:
                        break
                    msg = bjson.loads(raw_byte_msg)

                    if msg['type'] == T_REQUEST:
                        self.type_request(client=client, msg=msg)
                    elif msg['type'] == T_RESPONSE:
                        self.type_response(client=client, msg=msg)
                    elif msg['type'] == T_ACK:
                        self.type_ack(client=client, msg=msg)
                    else:
                        logging.debug("Unknown type %s" % msg['type'])

                except bjson.BJsonDecodeError:
                    self.p2p.remove_connection(client)
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    logging.debug("1:Processing error %s" % e)
                    logging.debug("2:Client %s" % client[3]['name'])
                    logging.debug("3:Byte msg \"%s\"" % raw_byte_msg.decode()[:90])
            # close client
            self.f_finish = True
            return

        logging.info("start command processing. I'm \"%s\"" % self.p2p.header['name'])
        if f_server:
            self.p2p.start()
        if f_stabilize:
            threading.Thread(
                target=self.stabilize, name='Stabilize', daemon=True
            ).start()
        # Processing
        threading.Thread(
            target=processing, name='Processing', daemon=True
        ).start()

    def type_request(self, client, msg):
        temperate = {
            'type': T_RESPONSE,
            'cmd': msg['cmd'],
            'data': None,
            'time': time.time(),
            'uuid': msg['uuid']}
        allow_list = list()
        deny_list = list()
        ack_list = list()

        if msg['cmd'] == ClientCmd.PING_PONG:
            temperate['data'] = {
                'ping': msg['data'],
                'pong': time.time()}
            allow_list.append(client)

        elif msg['cmd'] == ClientCmd.BROADCAST:
            if self.result.include(msg['uuid']):
                return  # already get broadcast data
            elif msg['uuid'] in self.waiting_ack:
                return  # I'm broadcaster, get from ack
            elif not self.broadcast_check(self, msg['data']):
                return  # not allow broadcast condition
            self.result.put(uuid=msg['uuid'], item=(client, msg))
            self.broadcast_que.broadcast(item=(client, msg))
            deny_list.append(client)
            allow_list = None
            # send ACK
            ack_list.append(client)
            # send Response
            temperate['type'] = T_REQUEST
            temperate['data'] = msg['data']

        elif msg['cmd'] == ClientCmd.GET_PEER_INFO:
            temperate['data'] = self.p2p.header
            allow_list.append(client)

        elif msg['cmd'] == ClientCmd.GET_PEERS:
            near = {(host_port[0], header['p2p_port']): header
                    for n, s, host_port, header, a, s_t in self.p2p.client}
            peer = {host_port: self.peers[host_port]['header'] for host_port in self.peers}
            temperate['data'] = {'near': list(near.items()), 'peer': list(peer.items())}
            # {"near": [[(host, port), header], ..],"peer": ...}
            allow_list.append(client)

        elif msg['cmd'] == ClientCmd.CHECK_REACHABLE:
            f_by_user_req = 'port' in msg['data'] and msg['data']['port']
            check_port = msg['data']['port'] if f_by_user_req else client[3]['p2p_port']
            temperate['data'] = is_reachable(host=client[2][0], port=check_port)
            allow_list.append(client)

        elif msg['cmd'] == ClientCmd.FILE_CHECK:
            file_hash = msg['data']['hash']
            asked_id = msg['data']['uuid']
            file_path = os.path.join(self.tmp_dir, 'file.' + file_hash + '.dat')
            f_existence = os.path.exists(file_path)
            f_already_asked = self.file_client_path.include(uuid=asked_id)
            temperate['data'] = {'have': f_existence, 'asked': f_already_asked}
            allow_list.append(client)

        elif msg['cmd'] == ClientCmd.FILE_GET:
            def asking():
                # ファイル要求元のNodeに近いNode群を無視する
                nears = [self.p2p.peer_format2client(host_port) for host_port in already_asked]
                nears = [c for c in nears if c is not None]  # Noneを除く
                random.shuffle(nears)
                # nearを最後に探索するように並び替え
                client_ = copy.copy(self.p2p.client)
                random.shuffle(client_)
                for c in copy.copy(client_):
                    if c in nears:
                        client_.remove(c)
                client_ += nears  # 最後に付け足し

                # ファイル所持Nodeを見つけたら即コマンド送る、それ以外は候補をリスト化
                candidates = list()
                for ask_client in client_:
                    try:
                        dummy, data = self.send_command(
                            cmd=ClientCmd.FILE_CHECK, client=ask_client, data={'hash': file_hash, 'uuid': msg['uuid']})
                    except Exception as e:
                        logging.debug("Check file existence one by one, %s", e)
                        continue
                    if data['have'] is True:
                        # ファイル所持Nodeを発見したのでGETを即送信
                        hopeful = ask_client
                        break
                    elif data['asked'] is False:
                        candidates.append(ask_client)
                    else:
                        pass
                else:
                    # 候補がいなければここで探索終了
                    if len(candidates) == 0:
                        temperate['type'] = T_RESPONSE
                        self._send_msg(msg=temperate, allow=[client], deny=list())
                        logging.debug("Asking, stop asking file.")
                        return
                    else:
                        hopeful = candidates[0]  # 一番新しいのを候補

                logging.debug("Asking, Candidate=%d, hopeful=\"%s\"" % (len(candidates), hopeful[3]['name']))
                try:
                    my_peers = [self.p2p.client2peer_format(c, dict())[0] for c in self.p2p.client]
                    data = {'hash': file_hash, 'asked': my_peers}
                    self.file_client_path.put(uuid=msg['uuid'], item=(client, hopeful))
                    from_client, data = self.send_command(cmd=ClientCmd.FILE_GET, data=data, client=hopeful, wait=20)
                    temperate['data'] = data
                    if data is None:
                        logging.debug("Get null from \"%s\" 0x%s" % (from_client[3]['name'], file_hash))
                    else:
                        logging.debug("Success get file from \"%s\" 0x%s" % (from_client[3]['name'], file_hash))
                except:
                    logging.debug("Failed to get file 0x%s, %s" % (file_hash, hopeful[3]['name']))
                    temperate['data'] = None
                temperate['type'] = T_RESPONSE
                count = self._send_msg(msg=temperate, allow=[client], deny=list())
                logging.debug("Response file to \"%s\" (%d)" % (client[3]['name'], count))
                return

            def sending():
                with open(file_path, mode='br') as f:
                    raw = f.read()
                temperate['type'] = T_RESPONSE
                temperate['data'] = raw
                self.file_client_path.put(uuid=msg['uuid'], item=(client, client))
                count = self._send_msg(msg=temperate, allow=[client], deny=list())
                logging.debug("Sending file to \"%s\" (%d)" % (client[3]['name'], count))
                return

            logging.debug("Asked file get by \"%s\"" % client[3]['name'])
            file_hash = msg['data']['hash']
            already_asked = [tuple(host_port) for host_port in msg['data']['asked']]
            file_path = os.path.join(self.tmp_dir, 'file.' + file_hash + '.dat')
            # When you have file, send. When you don't have file, asking
            if os.path.exists(file_path):
                threading.Thread(target=sending, name='Sending', daemon=True).start()
            else:
                threading.Thread(target=asking, name='Asking', daemon=True).start()
            # Don't send anyone at this time

        elif msg['cmd'] == ClientCmd.FILE_DELETE:
            if 'raw' not in msg['data'] or\
                    'sign' not in msg['data'] or\
                    'pem' not in msg['data']:
                return
            file_hash, time_ = bjson.loads(msg['data']['raw'])
            if abs(time.time()-time_) > 30:
                return  # old signature
            if self.result.include(msg['uuid']):
                return  # already get broadcast data
            elif msg['uuid'] in self.waiting_ack:
                return  # I'm broadcaster, get from ack
            self.result.put(uuid=msg['uuid'], item=(client, msg))
            deny_list.append(client)
            allow_list = None
            # send ACK
            ack_list.append(client)
            # send Response
            temperate['type'] = T_REQUEST
            temperate['data'] = msg['data']
            # delete file check
            try:
                logging.debug("1:Delete request 0x%s" % file_hash)
                work_dir = os.path.dirname(os.path.abspath(__file__))
                public_pem = open(os.path.join(work_dir, 'pem', msg['data']['pem']), mode='r').read()
                EncryptRSA.verify(public_pem, msg['data']['raw'], msg['data']['sign'])
                if self.remove_file(file_hash):
                    logging.info("2:Delete request accepted.")
            except ValueError:
                allow_list = list()  # No sending

        elif msg['cmd'] == ClientCmd.DIRECT_CMD:
            def direct_cmd():
                c = 200
                while c > 0:
                    time.sleep(0.02)
                    if self.direct_cmd_result.include(msg['uuid']):
                        temperate['data'] = self.direct_cmd_result.get(msg['uuid'])
                        break
                else:
                    temperate['data'] = None
                temperate['time'] = time.time()
                self._send_msg(msg=temperate, allow=[client])
            self.direct_cmd_que.broadcast(item=(msg['uuid'], msg['data']))
            threading.Thread(target=direct_cmd, name='DirectCmd', daemon=True).start()
        else:
            pass

        # send message
        send_count = self._send_msg(msg=temperate, allow=allow_list, deny=deny_list)
        # send ack
        ack_count = 0
        if len(ack_list) > 0:
            temperate['type'] = T_ACK
            temperate['data'] = send_count
            ack_count = self._send_msg(msg=temperate, allow=ack_list)
        # garbage correction
        if len(self.result.uuid2data) > self.p2p.listen * 100:
            self.result.del_old()
        if len(self.direct_cmd_result.uuid2data) > self.p2p.listen * 100:
            self.direct_cmd_result.del_old()
        if len(self.file_client_path.uuid2data) > self.p2p.listen * 100:
            self.file_client_path.del_old()
        if len(self.waiting_ack) > 50:
            self.waiting_ack = self.waiting_ack[25:]
        # debug
        # logging.debug("All=%d, Send=%d, Ack=%d" % (len(self.p2p.client), send_count, ack_count))

    def type_response(self, client, msg):
        uuid = msg['uuid']
        item = msg['data']
        cmd = msg['cmd']
        if cmd == ClientCmd.FILE_GET:
            # origin check
            if self.file_client_path.include(uuid):
                ship_from, ship_to = self.file_client_path.get(uuid)
                if ship_to != client:
                    logging.info("Error, origin is different from \"%s\", "
                                 "responded from \"%s\"" % (ship_to[3]['name'], client[3]['name']))
                    return
                else:
                    logging.debug("File get origin check OK.")

        if not self.result.include(uuid=uuid):
            self.result.put(uuid=uuid, item=(client, item))
            logging.debug("1:Get response. cmd=%s, uuid=%d, num=%d" % (cmd, uuid, client[0]))
            logging.debug("2:Data is \"%s\"" % trim_msg(item, 80))

    def type_ack(self, client, msg):
        uuid = msg['uuid']
        if not self.result.include(uuid=uuid):
            self.result.put(uuid=uuid, item=(client, msg['data']))
            logging.debug("Get ack from \"%s\"" % client[3]['name'])

    def _send_msg(self, msg, allow=None, deny=None):
        msg_body = bjson.dumps(msg)
        if allow is None:
            allow = self.p2p.client
        if deny is None:
            deny = list()

        c = 0
        for client in allow:
            if client not in deny:
                try:
                    self.p2p.send_msg(msg_body=msg_body, client=client)
                except Exception as e:
                    logging.debug("Failed send msg to \"%s\", %s" % (client[3]['name'], e))
                    continue
                # logging.debug("Response to \"%s\"" % client[3]['name'])
                c += 1
        return c

    def send_command(self, cmd, data=None, client=None, wait=5):
        uuid = random.randint(100000000, 999999999)
        temperate = {
            'type': T_REQUEST,
            'cmd': cmd,
            'data': data,
            'time': time.time(),
            'uuid': uuid}
        if len(self.p2p.client) == 0:
            raise ConnectionError('No client connection.')
        elif cmd == ClientCmd.BROADCAST:
            clients = self.p2p.client
            self.waiting_ack.append(uuid)
        elif cmd == ClientCmd.FILE_DELETE:
            clients = self.p2p.client
            self.waiting_ack.append(uuid)
        elif cmd == ClientCmd.FILE_GET:
            self.file_client_path.put(uuid=uuid, item=('I\'m Sender.', client))
            assert client is not None, 'You need select client by manually.'
            assert client in self.p2p.client, 'Unknown client.'
            wait = 20
            clients = [client]
        elif client is None:
            client = random.choice(self.p2p.client)
            clients = [client]
        elif client in self.p2p.client:
            clients = [client]
        else:
            raise ConnectionError("Not found client")
        self._send_msg(msg=temperate, allow=clients)
        # wait for response
        if wait < 1:
            raise ConnectionError("Need to wait cmd finish.")
        span = 0.01
        for i in range(int(wait / span)):
            time.sleep(span)
            if self.result.include(uuid=uuid):
                client, msg = self.result.get(uuid)
                if cmd == ClientCmd.BROADCAST:
                    self.broadcast_que.broadcast(item=(client, temperate))
                    return client, data
                else:
                    client, msg = self.result.get(uuid)
                    return client, msg
        else:
            self.p2p.remove_connection(client)
            name = len(clients) if client is None else client[3]['name']
            raise TimeoutError((cmd, data, uuid, name))

    def share_file(self, data):
        assert type(data) == bytes, "You need input raw binary data"
        assert len(data) < MAX_RECEIVE_SIZE + 1000, "Your data %dKb exceed MAX (%dKb) size." % \
                                             (len(data) // 1000, MAX_RECEIVE_SIZE // 1000)
        file_hash = sha256(data).hexdigest()
        file_path = os.path.join(self.tmp_dir, 'file.' + file_hash + '.dat')
        with open(file_path, mode='bw') as f:
            f.write(data)
        return file_hash

    def get_file(self, file_hash, only_check=False):
        file_hash = file_hash.lower()
        file_path = os.path.join(self.tmp_dir, 'file.' + file_hash + '.dat')
        if os.path.exists(file_path):
            return True if only_check else open(file_path, mode='br').read()
        else:
            # Ask all near nodes
            if len(self.p2p.client) == 0:
                raise FileReceiveError('No client found.')
            client_ = copy.copy(self.p2p.client)
            random.shuffle(client_)
            for client in client_:
                dummy, msg = self.send_command(cmd=ClientCmd.FILE_CHECK, data={'hash': file_hash, 'uuid': 0}, client=client)
                if msg['have']:
                    hopeful = client
                    break
            else:
                hopeful = random.choice(self.p2p.client)

            nears = [self.p2p.client2peer_format(c, dict())[0] for c in self.p2p.client]
            logging.debug("Ask file send to \"%s\"" % hopeful[3]['name'])
            dummy, raw = self.send_command(
                cmd=ClientCmd.FILE_GET, data={'hash': file_hash, 'asked': nears}, client=hopeful)
            if raw is None:
                raise FileReceiveError('Peers send me Null data. Please retry.')
            if sha256(raw).hexdigest() == file_hash:
                with open(file_path, mode='bw') as f:
                    f.write(raw)
                return True if only_check else raw
            else:
                raise FileReceiveError('File hash don\'t match. Please retry.')

    def remove_file(self, file_hash):
        file_hash = file_hash.lower()
        file_path = os.path.join(self.tmp_dir, 'file.' + file_hash + '.dat')
        try:
            os.remove(file_path)
            return True
        except:
            return False

    def remove_file_by_master(self, sk, file_hash, pem='public.master.pem', pwd=None):
        file_hash = file_hash.lower()
        file_path = os.path.join(self.tmp_dir, 'file.' + file_hash + '.dat')
        try:
            os.remove(file_path)
        except:
            pass
        raw = bjson.dumps((file_hash, time.time()), False)
        sign = EncryptRSA.sign(sk, raw, pwd=pwd)
        self.send_command(cmd=ClientCmd.FILE_DELETE, data={'raw': raw, 'sign': sign, 'pem': pem})
        logging.debug("Success delete file by master.")

    def stabilize(self):
        time.sleep(5)
        logging.info("start stabilize.")
        if len(self.peers) == 0:
            logging.error("peer list is zero, need bootnode.")
        else:
            need = max(1, self.p2p.listen // 2)
            logging.info("Connect first nodes, min %d clients." % need)
            peer_key = list(self.peers)
            random.shuffle(peer_key)
            for host_port in peer_key:
                if self.peers[host_port]['header']['p2p_accept']:
                    if self.p2p.create_connection(host=host_port[0], port=host_port[1]):
                        need -= 1
                if need <= 0:
                    break
                else:
                    time.sleep(5)

        # Stabilize
        peer_score = dict()
        near_info = dict()
        near_score = dict()
        count = 0
        need_connection = 3
        while not self.f_stop:
            count += 1
            if len(self.p2p.client) < need_connection:
                time.sleep(2)
            elif count % 24 == 0:
                time.sleep(10 * (1 + random.random()))
            else:
                time.sleep(5)
                continue
            try:
                if len(self.p2p.client) == 0 and len(self.peers) > 0:
                    host_port = random.choice(list(self.peers))
                    if self.p2p.create_connection(host_port[0], host_port[1]):
                        time.sleep(5)
                    else:
                        continue
                elif len(self.p2p.client) == 0 and len(self.peers) == 0:
                    time.sleep(10)
                    continue

                # peer list update (client)
                for client in self.p2p.client:
                    k, v = self.p2p.client2peer_format(client, self.peers)
                    self.peers[k] = v

                # ignore list
                p2p_port = self.p2p.header['p2p_port']
                ignore_node = [(GLOBAL_IP, p2p_port), (LOCAL_IP, p2p_port), ('127.0.0.1', p2p_port)]
                ignore_node += [(client[2][0], client[3]['p2p_port']) for client in self.p2p.client]

                # get near info
                client, msg = self.send_command(cmd=ClientCmd.GET_PEERS)
                logging.debug("Ask \"%s\" Peer list" % client[3]['name'])
                k, v = self.p2p.client2peer_format(client, self.peers)
                if k not in near_info:
                    near_info[k] = dict()
                    near_score[k] = len(msg['near'])
                for host_port, header in msg['near']:
                    host_port = tuple(host_port)
                    if host_port in ignore_node:
                        continue
                    if host_port in near_info[k]:
                        near_info[k][host_port] = header
                    else:
                        near_info[k] = {host_port: header}
                    score = self.peers[host_port]['score'] if host_port in self.peers else 0
                    self.peers[host_port] = {'header': header, 'score': score}

                # Recode
                if count % 20 == 1:
                    recode = dict()
                    for client in self.p2p.client:
                        k, v = self.p2p.client2peer_format(client, self.peers)
                        recode[k] = v
                    recode.update(self.peers)
                    self.update_peers(updates=recode)

                # Calculate score (higher score = low priority)
                # {(host, port): score, ...}
                peer_score = {host_port: 0 + (peer_score[host_port] if host_port in peer_score else 0)
                              for host_port in self.peers if self.peers[host_port]['header']['p2p_accept'] and
                              host_port not in ignore_node}
                for host_port in self.peers:
                    if host_port not in peer_score:
                        continue
                    if host_port in near_info:
                        peer_score[host_port] = len(near_info[host_port])
                    if host_port in near_score:
                        peer_score[host_port] += near_score[host_port] // 2
                # Remove already connected and same root node
                logging.debug("PeerScore %s" % peer_score)

                # Action join or remove or nothing
                if len(self.p2p.client) > self.p2p.listen * 2 // 3:
                    # Remove
                    if len(self.p2p.client) == 0:
                        continue
                    client_score = {(client[2][0], client[3]['p2p_port']): 0 for client in self.p2p.client}
                    sorted_client = sorted(client_score.items(), key=lambda x: x[1])
                    host_port, score = random.choice(sorted_client[len(sorted_client) // 2:])
                    client = self.p2p.peer_format2client(k=host_port)
                    # Check number of peers
                    client, msg = self.send_command(cmd=ClientCmd.GET_PEERS, client=client)
                    if len(msg['near']) >= need_connection:
                        self.p2p.remove_connection(client)
                        logging.debug("Remove connection %s:%d=%d" % (host_port[0], host_port[1], score))
                    elif host_port in peer_score:
                        peer_score[host_port] -= 1
                    else:
                        pass

                elif len(self.p2p.client) < self.p2p.listen * 2 // 3:
                    # Join
                    if len(peer_score) == 0:
                        continue
                    sorted_client = sorted(peer_score.items(), key=lambda x: x[1])
                    host_port, score = random.choice(sorted_client[:len(sorted_client) // 2 + 1])
                    if not self.p2p.create_connection(host=host_port[0], port=host_port[1]):
                        logging.info("Failed connect, rank down (%s:%d)" % (host_port[0], host_port[1]))
                        peer_score[host_port] += 1  # self.peers.remove(host_port)  # or reduce score?
                        if peer_score[host_port] > self.p2p.listen:
                            del self.peers[host_port]
                            self.update_peers(updates=self.peers)

                elif len(self.p2p.client) > self.p2p.listen // 2 and random.random() < 0.01:
                    # Mutation
                    client = random.choice(self.p2p.client)
                    self.p2p.remove_connection(client)
                    logging.debug("Mutate connection")

                else:
                    time.sleep(60)  # Do nothing

            except TimeoutError as e:
                logging.info("Cmd timeout %s" % e)
            except Exception as e:
                logging.debug("Stabilize %s" % e, exc_info=True)

    @staticmethod
    def broadcast_check(pc, data):
        return False  # overwrite


class FileReceiveError(FileExistsError): pass
