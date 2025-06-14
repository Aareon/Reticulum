# Reticulum License
#
# Copyright (c) 2016-2025 Mark Qvist
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# - The Software shall not be used in any kind of system which includes amongst
#   its functions the ability to purposefully do harm to human beings.
#
# - The Software shall not be used, directly or indirectly, in the creation of
#   an artificial intelligence, machine learning or language model training
#   dataset, including but not limited to any use that contributes to the
#   training or development of such a model or algorithm.
#
# - The above copyright notice and this permission notice shall be included in
#   all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from RNS.Cryptography import X25519PrivateKey, X25519PublicKey, Ed25519PrivateKey, Ed25519PublicKey
from RNS.Cryptography import Token
from RNS.Channel import Channel, LinkChannelOutlet
from RNS import Reticulum

from time import sleep
from .vendor import umsgpack as umsgpack
import threading
import inspect
import struct
import math
import time
import RNS
import io

class LinkCallbacks:
    def __init__(self):
        self.link_established = None
        self.link_closed = None
        self.packet = None
        self.resource = None
        self.resource_started = None
        self.resource_concluded = None
        self.remote_identified = None

class Link:
    """
    This class is used to establish and manage links to other peers. When a
    link instance is created, Reticulum will attempt to establish verified
    and encrypted connectivity with the specified destination.

    :param destination: A :ref:`RNS.Destination<api-destination>` instance which to establish a link to.
    :param established_callback: An optional function or method with the signature *callback(link)* to be called when the link has been established.
    :param closed_callback: An optional function or method with the signature *callback(link)* to be called when the link is closed.
    """
    CURVE = RNS.Identity.CURVE
    """
    The curve used for Elliptic Curve DH key exchanges
    """

    ECPUBSIZE         = 32+32
    KEYSIZE           = 32

    MDU = math.floor((RNS.Reticulum.MTU-RNS.Reticulum.IFAC_MIN_SIZE-RNS.Reticulum.HEADER_MINSIZE-RNS.Identity.TOKEN_OVERHEAD)/RNS.Identity.AES128_BLOCKSIZE)*RNS.Identity.AES128_BLOCKSIZE - 1

    ESTABLISHMENT_TIMEOUT_PER_HOP = RNS.Reticulum.DEFAULT_PER_HOP_TIMEOUT
    """
    Timeout for link establishment in seconds per hop to destination.
    """

    LINK_MTU_SIZE            = 3
    TRAFFIC_TIMEOUT_MIN_MS   = 5
    TRAFFIC_TIMEOUT_FACTOR   = 6
    KEEPALIVE_MAX_RTT        = 1.75
    KEEPALIVE_TIMEOUT_FACTOR = 4
    """
    RTT timeout factor used in link timeout calculation.
    """
    STALE_GRACE = 5
    """
    Grace period in seconds used in link timeout calculation.
    """
    KEEPALIVE_MAX = 360
    KEEPALIVE_MIN = 5
    KEEPALIVE     = KEEPALIVE_MAX
    """
    Default interval for sending keep-alive packets on established links in seconds.
    """
    STALE_FACTOR = 2
    STALE_TIME = STALE_FACTOR*KEEPALIVE
    """
    If no traffic or keep-alive packets are received within this period, the
    link will be marked as stale, and a final keep-alive packet will be sent.
    If after this no traffic or keep-alive packets are received within ``RTT`` *
    ``KEEPALIVE_TIMEOUT_FACTOR`` + ``STALE_GRACE``, the link is considered timed out,
    and will be torn down.
    """

    WATCHDOG_MAX_SLEEP  = 5

    PENDING             = 0x00
    HANDSHAKE           = 0x01
    ACTIVE              = 0x02
    STALE               = 0x03
    CLOSED              = 0x04

    TIMEOUT             = 0x01
    INITIATOR_CLOSED    = 0x02
    DESTINATION_CLOSED  = 0x03

    ACCEPT_NONE         = 0x00
    ACCEPT_APP          = 0x01
    ACCEPT_ALL          = 0x02
    resource_strategies = [ACCEPT_NONE, ACCEPT_APP, ACCEPT_ALL]

    MODE_AES128_CBC     = 0x00
    MODE_AES256_CBC     = 0x01
    MODE_AES256_GCM     = 0x02
    MODE_OTP_RESERVED   = 0x03
    MODE_PQ_RESERVED_1  = 0x04
    MODE_PQ_RESERVED_2  = 0x05
    MODE_PQ_RESERVED_3  = 0x06
    MODE_PQ_RESERVED_4  = 0x07
    ENABLED_MODES       = [MODE_AES256_CBC]
    MODE_DEFAULT        =  MODE_AES256_CBC
    MODE_DESCRIPTIONS   = {MODE_AES128_CBC: "AES_128_CBC",
                           MODE_AES256_CBC: "AES_256_CBC",
                           MODE_AES256_GCM: "MODE_AES256_GCM",
                           MODE_OTP_RESERVED: "MODE_OTP_RESERVED",
                           MODE_PQ_RESERVED_1: "MODE_PQ_RESERVED_1",
                           MODE_PQ_RESERVED_2: "MODE_PQ_RESERVED_2",
                           MODE_PQ_RESERVED_3: "MODE_PQ_RESERVED_3",
                           MODE_PQ_RESERVED_4: "MODE_PQ_RESERVED_4"}

    MTU_BYTEMASK        = 0x1FFFFF
    MODE_BYTEMASK       = 0xE0

    @staticmethod
    def signalling_bytes(mtu, mode):
        if not mode in Link.ENABLED_MODES: raise TypeError(f"Requested link mode {Link.MODE_DESCRIPTIONS[mode]} not enabled")
        signalling_value = (mtu & Link.MTU_BYTEMASK)+(((mode<<5) & Link.MODE_BYTEMASK)<<16)
        return struct.pack(">I", signalling_value)[1:]

    @staticmethod
    def mtu_from_lr_packet(packet):
        if len(packet.data) == Link.ECPUBSIZE+Link.LINK_MTU_SIZE:
            return (packet.data[Link.ECPUBSIZE] << 16) + (packet.data[Link.ECPUBSIZE+1] << 8) + (packet.data[Link.ECPUBSIZE+2]) & Link.MTU_BYTEMASK
        else: return None

    @staticmethod
    def mtu_from_lp_packet(packet):
        if len(packet.data) == RNS.Identity.SIGLENGTH//8+Link.ECPUBSIZE//2+Link.LINK_MTU_SIZE:
            mtu_bytes = packet.data[RNS.Identity.SIGLENGTH//8+Link.ECPUBSIZE//2:RNS.Identity.SIGLENGTH//8+Link.ECPUBSIZE//2+Link.LINK_MTU_SIZE]
            return (mtu_bytes[0] << 16) + (mtu_bytes[1] << 8) + (mtu_bytes[2]) & Link.MTU_BYTEMASK
        else: return None

    @staticmethod
    def mode_byte(mode):
        if mode in Link.ENABLED_MODES: return (mode << 5) & Link.MODE_BYTEMASK
        else: raise TypeError(f"Requested link mode {mode} not enabled")

    @staticmethod
    def mode_from_lr_packet(packet):
        if len(packet.data) > Link.ECPUBSIZE:
            mode = (packet.data[Link.ECPUBSIZE] & Link.MODE_BYTEMASK) >> 5
            return mode
        else: return Link.MODE_DEFAULT

    @staticmethod
    def mode_from_lp_packet(packet):
        if len(packet.data) > RNS.Identity.SIGLENGTH//8+Link.ECPUBSIZE//2:
            mode = packet.data[RNS.Identity.SIGLENGTH//8+Link.ECPUBSIZE//2] >> 5
            return mode
        else: return Link.MODE_DEFAULT

    @staticmethod
    def validate_request(owner, data, packet):
        if len(data) == Link.ECPUBSIZE or len(data) == Link.ECPUBSIZE+Link.LINK_MTU_SIZE:
            try:
                link = Link(owner = owner, peer_pub_bytes=data[:Link.ECPUBSIZE//2], peer_sig_pub_bytes=data[Link.ECPUBSIZE//2:Link.ECPUBSIZE])
                link.set_link_id(packet)

                if len(data) == Link.ECPUBSIZE+Link.LINK_MTU_SIZE:
                    RNS.log("Link request includes MTU signalling", RNS.LOG_DEBUG) # TODO: Remove debug
                    try:
                        link.mtu = Link.mtu_from_lr_packet(packet) or Reticulum.MTU
                    except Exception as e:
                        RNS.trace_exception(e)
                        link.mtu = RNS.Reticulum.MTU

                link.mode = Link.mode_from_lr_packet(packet)
                
                # TODO: Remove debug
                RNS.log(f"Incoming link request with mode {Link.MODE_DESCRIPTIONS[link.mode]}", RNS.LOG_DEBUG)

                link.update_mdu()
                link.destination = packet.destination
                link.establishment_timeout = Link.ESTABLISHMENT_TIMEOUT_PER_HOP * max(1, packet.hops) + Link.KEEPALIVE
                link.establishment_cost += len(packet.raw)
                RNS.log(f"Validating link request {RNS.prettyhexrep(link.link_id)}", RNS.LOG_DEBUG)
                RNS.log(f"Link MTU configured to {RNS.prettysize(link.mtu)}", RNS.LOG_EXTREME)
                RNS.log(f"Establishment timeout is {RNS.prettytime(link.establishment_timeout)} for incoming link request "+RNS.prettyhexrep(link.link_id), RNS.LOG_EXTREME)
                link.handshake()
                link.attached_interface = packet.receiving_interface
                link.prove()
                link.request_time = time.time()
                RNS.Transport.register_link(link)
                link.last_inbound = time.time()
                link.__update_phy_stats(packet, force_update=True)
                link.start_watchdog()

                RNS.log("Incoming link request "+str(link)+" accepted on "+str(link.attached_interface), RNS.LOG_DEBUG)
                return link

            except Exception as e:
                RNS.log(f"Validating link request failed: {e}", RNS.LOG_VERBOSE)
                return None

        else:
            RNS.log(f"Invalid link request payload size of {len(data)} bytes, dropping request", RNS.LOG_DEBUG)
            return None


    def __init__(self, destination=None, established_callback=None, closed_callback=None, owner=None, peer_pub_bytes=None, peer_sig_pub_bytes=None, mode=MODE_DEFAULT):
        if destination != None and destination.type != RNS.Destination.SINGLE: raise TypeError("Links can only be established to the \"single\" destination type")
        self.mode = mode
        self.rtt = None
        self.mtu = RNS.Reticulum.MTU
        self.establishment_cost = 0
        self.establishment_rate = None
        self.expected_rate = None
        self.callbacks = LinkCallbacks()
        self.resource_strategy = Link.ACCEPT_NONE
        self.last_resource_window = None
        self.last_resource_eifr = None
        self.outgoing_resources = []
        self.incoming_resources = []
        self.pending_requests   = []
        self.last_inbound = 0
        self.last_outbound = 0
        self.last_keepalive = 0
        self.last_proof = 0
        self.last_data = 0
        self.tx = 0
        self.rx = 0
        self.txbytes = 0
        self.rxbytes = 0
        self.rssi = None
        self.snr = None
        self.q = None
        self.traffic_timeout_factor = Link.TRAFFIC_TIMEOUT_FACTOR
        self.keepalive_timeout_factor = Link.KEEPALIVE_TIMEOUT_FACTOR
        self.keepalive = Link.KEEPALIVE
        self.stale_time = Link.STALE_TIME
        self.watchdog_lock = False
        self.status = Link.PENDING
        self.activated_at = None
        self.type = RNS.Destination.LINK
        self.owner = owner
        self.destination = destination
        self.expected_hops = None
        self.attached_interface = None
        self.__remote_identity = None
        self.__track_phy_stats = False
        self._channel = None

        if self.destination == None:
            self.initiator = False
            self.prv     = X25519PrivateKey.generate()
            self.sig_prv = self.owner.identity.sig_prv
        else:
            self.initiator = True
            self.expected_hops = RNS.Transport.hops_to(self.destination.hash)
            self.establishment_timeout  = RNS.Reticulum.get_instance().get_first_hop_timeout(destination.hash)
            self.establishment_timeout += Link.ESTABLISHMENT_TIMEOUT_PER_HOP * max(1, RNS.Transport.hops_to(destination.hash))
            self.prv     = X25519PrivateKey.generate()
            self.sig_prv = Ed25519PrivateKey.generate()

        self.token  = None
        
        self.pub = self.prv.public_key()
        self.pub_bytes = self.pub.public_bytes()

        self.sig_pub = self.sig_prv.public_key()
        self.sig_pub_bytes = self.sig_pub.public_bytes()

        if peer_pub_bytes == None:
            self.peer_pub = None
            self.peer_pub_bytes = None
        else:
            self.load_peer(peer_pub_bytes, peer_sig_pub_bytes)

        if established_callback != None:
            self.set_link_established_callback(established_callback)

        if closed_callback != None:
            self.set_link_closed_callback(closed_callback)

        if self.initiator:
            signalling_bytes = b""
            nh_hw_mtu = RNS.Transport.next_hop_interface_hw_mtu(destination.hash)
            if RNS.Reticulum.link_mtu_discovery() and nh_hw_mtu:
                signalling_bytes = Link.signalling_bytes(nh_hw_mtu, self.mode)
                RNS.log(f"Signalling link MTU of {RNS.prettysize(nh_hw_mtu)} for link", RNS.LOG_DEBUG) # TODO: Remove debug
            else: signalling_bytes = Link.signalling_bytes(RNS.Reticulum.MTU, self.mode)
            RNS.log(f"Establishing link with mode {Link.MODE_DESCRIPTIONS[self.mode]}", RNS.LOG_DEBUG) # TODO: Remove debug
            self.request_data = self.pub_bytes+self.sig_pub_bytes+signalling_bytes
            self.packet = RNS.Packet(destination, self.request_data, packet_type=RNS.Packet.LINKREQUEST)
            self.packet.pack()
            self.establishment_cost += len(self.packet.raw)
            self.set_link_id(self.packet)
            RNS.Transport.register_link(self)
            self.request_time = time.time()
            self.start_watchdog()
            self.packet.send()
            self.had_outbound()
            RNS.log("Link request "+RNS.prettyhexrep(self.link_id)+" sent to "+str(self.destination), RNS.LOG_DEBUG)
            RNS.log(f"Establishment timeout is {RNS.prettytime(self.establishment_timeout)} for link request "+RNS.prettyhexrep(self.link_id), RNS.LOG_EXTREME)


    def load_peer(self, peer_pub_bytes, peer_sig_pub_bytes):
        self.peer_pub_bytes = peer_pub_bytes
        self.peer_pub = X25519PublicKey.from_public_bytes(self.peer_pub_bytes)

        self.peer_sig_pub_bytes = peer_sig_pub_bytes
        self.peer_sig_pub = Ed25519PublicKey.from_public_bytes(self.peer_sig_pub_bytes)

        if not hasattr(self.peer_pub, "curve"):
            self.peer_pub.curve = Link.CURVE

    @staticmethod
    def link_id_from_lr_packet(packet):
        hashable_part = packet.get_hashable_part()
        if len(packet.data) > Link.ECPUBSIZE:
            diff = len(packet.data) - Link.ECPUBSIZE
            hashable_part = hashable_part[:-diff]

        return RNS.Identity.truncated_hash(hashable_part)

    def set_link_id(self, packet):
        self.link_id = Link.link_id_from_lr_packet(packet)
        self.hash = self.link_id

    def handshake(self):
        if self.status == Link.PENDING and self.prv != None:
            self.status = Link.HANDSHAKE
            self.shared_key = self.prv.exchange(self.peer_pub)

            if   self.mode == Link.MODE_AES128_CBC: derived_key_length = 32
            elif self.mode == Link.MODE_AES256_CBC: derived_key_length = 64
            else: raise TypeError(f"Invalid link mode {self.mode} on {self}")

            self.derived_key = RNS.Cryptography.hkdf(
                length=derived_key_length,
                derive_from=self.shared_key,
                salt=self.get_salt(),
                context=self.get_context())

        else: RNS.log("Handshake attempt on "+str(self)+" with invalid state "+str(self.status), RNS.LOG_ERROR)


    def prove(self):
        signalling_bytes = Link.signalling_bytes(self.mtu, self.mode)
        signed_data = self.link_id+self.pub_bytes+self.sig_pub_bytes+signalling_bytes
        signature = self.owner.identity.sign(signed_data)

        proof_data = signature+self.pub_bytes+signalling_bytes
        proof = RNS.Packet(self, proof_data, packet_type=RNS.Packet.PROOF, context=RNS.Packet.LRPROOF)
        proof.send()
        self.establishment_cost += len(proof.raw)
        self.had_outbound()


    def prove_packet(self, packet):
        signature = self.sign(packet.packet_hash)
        # TODO: Hardcoded as explicit proof for now
        # if RNS.Reticulum.should_use_implicit_proof():
        #   proof_data = signature
        # else:
        #   proof_data = packet.packet_hash + signature
        proof_data = packet.packet_hash + signature

        proof = RNS.Packet(self, proof_data, RNS.Packet.PROOF)
        proof.send()
        self.had_outbound()

    def validate_proof(self, packet):
        try:
            if self.status == Link.PENDING:
                signalling_bytes = b""
                confirmed_mtu = None
                mode = Link.mode_from_lp_packet(packet)
                RNS.log(f"Validating link request proof with mode {Link.MODE_DESCRIPTIONS[mode]}", RNS.LOG_DEBUG) # TODO: Remove debug
                if mode != self.mode: raise TypeError(f"Invalid link mode {mode} in link request proof")
                if len(packet.data) == RNS.Identity.SIGLENGTH//8+Link.ECPUBSIZE//2+Link.LINK_MTU_SIZE:
                    confirmed_mtu = Link.mtu_from_lp_packet(packet)
                    signalling_bytes = Link.signalling_bytes(confirmed_mtu, mode)
                    packet.data = packet.data[:RNS.Identity.SIGLENGTH//8+Link.ECPUBSIZE//2]
                    RNS.log(f"Destination confirmed link MTU of {RNS.prettysize(confirmed_mtu)}", RNS.LOG_DEBUG) # TODO: Remove debug

                if self.initiator and len(packet.data) == RNS.Identity.SIGLENGTH//8+Link.ECPUBSIZE//2:
                    peer_pub_bytes = packet.data[RNS.Identity.SIGLENGTH//8:RNS.Identity.SIGLENGTH//8+Link.ECPUBSIZE//2]
                    peer_sig_pub_bytes = self.destination.identity.get_public_key()[Link.ECPUBSIZE//2:Link.ECPUBSIZE]
                    self.load_peer(peer_pub_bytes, peer_sig_pub_bytes)
                    self.handshake()

                    self.establishment_cost += len(packet.raw)
                    signed_data = self.link_id+self.peer_pub_bytes+self.peer_sig_pub_bytes+signalling_bytes
                    signature = packet.data[:RNS.Identity.SIGLENGTH//8]
                    
                    if self.destination.identity.validate(signature, signed_data):
                        if self.status != Link.HANDSHAKE:
                            raise IOError("Invalid link state for proof validation: "+str(self.status))

                        self.rtt = time.time() - self.request_time
                        self.attached_interface = packet.receiving_interface
                        self.__remote_identity = self.destination.identity
                        self.mtu = confirmed_mtu or RNS.Reticulum.MTU
                        self.update_mdu()
                        self.status = Link.ACTIVE
                        self.activated_at = time.time()
                        self.last_proof = self.activated_at
                        RNS.Transport.activate_link(self)
                        RNS.log("Link "+str(self)+" established with "+str(self.destination)+", RTT is "+RNS.prettyshorttime(self.rtt), RNS.LOG_DEBUG)
                        
                        if self.rtt != None and self.establishment_cost != None and self.rtt > 0 and self.establishment_cost > 0:
                            self.establishment_rate = self.establishment_cost/self.rtt

                        self.__update_keepalive()

                        rtt_data = umsgpack.packb(self.rtt)
                        rtt_packet = RNS.Packet(self, rtt_data, context=RNS.Packet.LRRTT)
                        rtt_packet.send()
                        self.had_outbound()
                        self.__update_phy_stats(packet)

                        if self.callbacks.link_established != None:
                            thread = threading.Thread(target=self.callbacks.link_established, args=(self,))
                            thread.daemon = True
                            thread.start()
                    else:
                        RNS.log("Invalid link proof signature received by "+str(self)+". Ignoring.", RNS.LOG_DEBUG)
        
        except Exception as e:
            self.status = Link.CLOSED
            RNS.log("An error ocurred while validating link request proof on "+str(self)+".", RNS.LOG_ERROR)
            RNS.log("The contained exception was: "+str(e), RNS.LOG_ERROR)


    def identify(self, identity):
        """
        Identifies the initiator of the link to the remote peer. This can only happen
        once the link has been established, and is carried out over the encrypted link.
        The identity is only revealed to the remote peer, and initiator anonymity is
        thus preserved. This method can be used for authentication.

        :param identity: An RNS.Identity instance to identify as.
        """
        if self.initiator and self.status == Link.ACTIVE:
            signed_data = self.link_id + identity.get_public_key()
            signature = identity.sign(signed_data)
            proof_data = identity.get_public_key() + signature

            proof = RNS.Packet(self, proof_data, RNS.Packet.DATA, context = RNS.Packet.LINKIDENTIFY)
            proof.send()
            self.had_outbound()


    def request(self, path, data = None, response_callback = None, failed_callback = None, progress_callback = None, timeout = None):
        """
        Sends a request to the remote peer.

        :param path: The request path.
        :param response_callback: An optional function or method with the signature *response_callback(request_receipt)* to be called when a response is received. See the :ref:`Request Example<example-request>` for more info.
        :param failed_callback: An optional function or method with the signature *failed_callback(request_receipt)* to be called when a request fails. See the :ref:`Request Example<example-request>` for more info.
        :param progress_callback: An optional function or method with the signature *progress_callback(request_receipt)* to be called when progress is made receiving the response. Progress can be accessed as a float between 0.0 and 1.0 by the *request_receipt.progress* property.
        :param timeout: An optional timeout in seconds for the request. If *None* is supplied it will be calculated based on link RTT.
        :returns: A :ref:`RNS.RequestReceipt<api-requestreceipt>` instance if the request was sent, or *False* if it was not.
        """
        request_path_hash = RNS.Identity.truncated_hash(path.encode("utf-8"))
        unpacked_request  = [time.time(), request_path_hash, data]
        packed_request    = umsgpack.packb(unpacked_request)

        if timeout == None:
            timeout = self.rtt * self.traffic_timeout_factor + RNS.Resource.RESPONSE_MAX_GRACE_TIME*1.125

        if len(packed_request) <= self.mdu:
            request_packet   = RNS.Packet(self, packed_request, RNS.Packet.DATA, context = RNS.Packet.REQUEST)
            packet_receipt   = request_packet.send()

            if packet_receipt == False:
                return False
            else:
                packet_receipt.set_timeout(timeout)
                return RequestReceipt(
                    self,
                    packet_receipt = packet_receipt,
                    response_callback = response_callback,
                    failed_callback = failed_callback,
                    progress_callback = progress_callback,
                    timeout = timeout,
                    request_size = len(packed_request),
                )
            
        else:
            request_id = RNS.Identity.truncated_hash(packed_request)
            RNS.log("Sending request "+RNS.prettyhexrep(request_id)+" as resource.", RNS.LOG_DEBUG)
            request_resource = RNS.Resource(packed_request, self, request_id = request_id, is_response = False, timeout = timeout)

            return RequestReceipt(
                self,
                resource = request_resource,
                response_callback = response_callback,
                failed_callback = failed_callback,
                progress_callback = progress_callback,
                timeout = timeout,
                request_size = len(packed_request),
            )


    def update_mdu(self):
        self.mdu = self.mtu - RNS.Reticulum.HEADER_MAXSIZE - RNS.Reticulum.IFAC_MIN_SIZE
        self.mdu = math.floor((self.mtu-RNS.Reticulum.IFAC_MIN_SIZE-RNS.Reticulum.HEADER_MINSIZE-RNS.Identity.TOKEN_OVERHEAD)/RNS.Identity.AES128_BLOCKSIZE)*RNS.Identity.AES128_BLOCKSIZE - 1

    def rtt_packet(self, packet):
        try:
            measured_rtt = time.time() - self.request_time
            plaintext = self.decrypt(packet.data)
            if plaintext != None:
                rtt = umsgpack.unpackb(plaintext)
                self.rtt = max(measured_rtt, rtt)
                self.status = Link.ACTIVE
                self.activated_at = time.time()

                if self.rtt != None and self.establishment_cost != None and self.rtt > 0 and self.establishment_cost > 0:
                    self.establishment_rate = self.establishment_cost/self.rtt

                self.__update_keepalive()

                try:
                    if self.owner.callbacks.link_established != None:
                            self.owner.callbacks.link_established(self)
                except Exception as e:
                    RNS.log("Error occurred in external link establishment callback. The contained exception was: "+str(e), RNS.LOG_ERROR)

        except Exception as e:
            RNS.log("Error occurred while processing RTT packet, tearing down link. The contained exception was: "+str(e), RNS.LOG_ERROR)
            self.teardown()

    def track_phy_stats(self, track):
        """
        You can enable physical layer statistics on a per-link basis. If this is enabled,
        and the link is running over an interface that supports reporting physical layer
        statistics, you will be able to retrieve stats such as *RSSI*, *SNR* and physical
        *Link Quality* for the link.

        :param track: Whether or not to keep track of physical layer statistics. Value must be ``True`` or ``False``.
        """
        if track:
            self.__track_phy_stats = True
        else:
            self.__track_phy_stats = False

    def get_rssi(self):
        """
        :returns: The physical layer *Received Signal Strength Indication* if available, otherwise ``None``. Physical layer statistics must be enabled on the link for this method to return a value.
        """
        if self.__track_phy_stats:
            return self.rssi
        else:
            return None

    def get_snr(self):
        """
        :returns: The physical layer *Signal-to-Noise Ratio* if available, otherwise ``None``. Physical layer statistics must be enabled on the link for this method to return a value.
        """
        if self.__track_phy_stats:
            return self.snr
        else:
            return None

    def get_q(self):
        """
        :returns: The physical layer *Link Quality* if available, otherwise ``None``. Physical layer statistics must be enabled on the link for this method to return a value.
        """
        if self.__track_phy_stats:
            return self.q
        else:
            return None

    def get_establishment_rate(self):
        """
        :returns: The data transfer rate at which the link establishment procedure ocurred, in bits per second.
        """
        if self.establishment_rate != None:
            return self.establishment_rate*8
        else:
            return None

    def get_mtu(self):
        """
        :returns: The MTU of an established link.
        """
        if self.status == Link.ACTIVE:
            return self.mtu
        else:
            return None

    def get_mdu(self):
        """
        :returns: The packet MDU of an established link.
        """
        if self.status == Link.ACTIVE:
            return self.mdu
        else:
            return None

    def get_expected_rate(self):
        """
        :returns: The packet expected in-flight data rate of an established link.
        """
        if self.status == Link.ACTIVE:
            return self.expected_rate
        else:
            return None

    def get_mode(self):
        """
        :returns: The mode of an established link.
        """
        return self.mode

    def get_salt(self):
        return self.link_id

    def get_context(self):
        return None

    def get_age(self):
        """
        :returns: The time in seconds since this link was established.
        """
        if self.activated_at:
            return time.time() - self.activated_at
        else:
            return None

    def no_inbound_for(self):
        """
        :returns: The time in seconds since last inbound packet on the link. This includes keepalive packets.
        """
        activated_at = self.activated_at if self.activated_at != None else 0
        last_inbound = max(self.last_inbound, activated_at)
        return time.time() - last_inbound

    def no_outbound_for(self):
        """
        :returns: The time in seconds since last outbound packet on the link. This includes keepalive packets.
        """
        return time.time() - self.last_outbound

    def no_data_for(self):
        """
        :returns: The time in seconds since payload data traversed the link. This excludes keepalive packets.
        """
        return time.time() - self.last_data

    def inactive_for(self):
        """
        :returns: The time in seconds since activity on the link. This includes keepalive packets.
        """
        return min(self.no_inbound_for(), self.no_outbound_for())

    def get_remote_identity(self):
        """
        :returns: The identity of the remote peer, if it is known. Calling this method will not query the remote initiator to reveal its identity. Returns ``None`` if the link initiator has not already independently called the ``identify(identity)`` method.
        """
        return self.__remote_identity

    def had_outbound(self, is_keepalive=False):
        self.last_outbound = time.time()
        if not is_keepalive: self.last_data = self.last_outbound
        else:                self.last_keepalive = self.last_outbound

    def __teardown_packet(self):
        teardown_packet = RNS.Packet(self, self.link_id, context=RNS.Packet.LINKCLOSE)
        teardown_packet.send()
        self.had_outbound()

    def teardown(self):
        """
        Closes the link and purges encryption keys. New keys will
        be used if a new link to the same destination is established.
        """
        if self.status != Link.PENDING and self.status != Link.CLOSED: self.__teardown_packet()
        self.status = Link.CLOSED
        if self.initiator: self.teardown_reason = Link.INITIATOR_CLOSED
        else: self.teardown_reason = Link.DESTINATION_CLOSED
        self.link_closed()

    def teardown_packet(self, packet):
        try:
            plaintext = self.decrypt(packet.data)
            if plaintext == self.link_id:
                self.status = Link.CLOSED
                if self.initiator:
                    self.teardown_reason = Link.DESTINATION_CLOSED
                else:
                    self.teardown_reason = Link.INITIATOR_CLOSED
                self.__update_phy_stats(packet)
                self.link_closed()
        except Exception as e:
            pass

    def link_closed(self):
        for resource in self.incoming_resources:
            resource.cancel()
        for resource in self.outgoing_resources:
            resource.cancel()
        if self._channel:
            self._channel._shutdown()
            
        self.prv = None
        self.pub = None
        self.pub_bytes = None
        self.shared_key = None
        self.derived_key = None

        if self.destination != None:
            if self.destination.direction == RNS.Destination.IN:
                if self in self.destination.links:
                    self.destination.links.remove(self)

        if self.callbacks.link_closed != None:
            try:
                self.callbacks.link_closed(self)
            except Exception as e:
                RNS.log("Error while executing link closed callback from "+str(self)+". The contained exception was: "+str(e), RNS.LOG_ERROR)


    def start_watchdog(self):
        thread = threading.Thread(target=self.__watchdog_job)
        thread.daemon = True
        thread.start()

    def __watchdog_job(self):
        while not self.status == Link.CLOSED:
            while (self.watchdog_lock):
                rtt_wait = 0.025
                if hasattr(self, "rtt") and self.rtt:
                    rtt_wait = self.rtt

                sleep(max(rtt_wait, 0.025))

            if not self.status == Link.CLOSED:
                # Link was initiated, but no response
                # from destination yet
                if self.status == Link.PENDING:
                    next_check = self.request_time + self.establishment_timeout
                    sleep_time = next_check - time.time()
                    if time.time() >= self.request_time + self.establishment_timeout:
                        RNS.log("Link establishment timed out", RNS.LOG_VERBOSE)
                        self.status = Link.CLOSED
                        self.teardown_reason = Link.TIMEOUT
                        self.link_closed()
                        sleep_time = 0.001

                elif self.status == Link.HANDSHAKE:
                    next_check = self.request_time + self.establishment_timeout
                    sleep_time = next_check - time.time()
                    if time.time() >= self.request_time + self.establishment_timeout:
                        self.status = Link.CLOSED
                        self.teardown_reason = Link.TIMEOUT
                        self.link_closed()
                        sleep_time = 0.001

                        if self.initiator:
                            RNS.log("Timeout waiting for link request proof", RNS.LOG_DEBUG)
                        else:
                            RNS.log("Timeout waiting for RTT packet from link initiator", RNS.LOG_DEBUG)

                elif self.status == Link.ACTIVE:
                    activated_at = self.activated_at if self.activated_at != None else 0
                    last_inbound = max(max(self.last_inbound, self.last_proof), activated_at)
                    now = time.time()

                    if now >= last_inbound + self.keepalive:
                        if self.initiator and now >= self.last_keepalive + self.keepalive:
                            self.send_keepalive()

                        if time.time() >= last_inbound + self.stale_time:
                            sleep_time = self.rtt * self.keepalive_timeout_factor + Link.STALE_GRACE
                            self.status = Link.STALE
                        else:
                            sleep_time = self.keepalive
                    
                    else:
                        sleep_time = (last_inbound + self.keepalive) - time.time()

                elif self.status == Link.STALE:
                    sleep_time = 0.001
                    self.__teardown_packet()
                    self.status = Link.CLOSED
                    self.teardown_reason = Link.TIMEOUT
                    self.link_closed()


                if sleep_time == 0:
                    RNS.log("Warning! Link watchdog sleep time of 0!", RNS.LOG_ERROR)
                if sleep_time == None or sleep_time < 0:
                    RNS.log("Timing error! Tearing down link "+str(self)+" now.", RNS.LOG_ERROR)
                    self.teardown()
                    sleep_time = 0.1

                sleep_time = min(sleep_time, Link.WATCHDOG_MAX_SLEEP)
                sleep(sleep_time)

                if not self.__track_phy_stats:
                    self.rssi = None
                    self.snr  = None
                    self.q    = None


    def __update_phy_stats(self, packet, query_shared = True, force_update = False):
        if self.__track_phy_stats or force_update:
            if query_shared:
                reticulum = RNS.Reticulum.get_instance()
                if packet.rssi == None: packet.rssi = reticulum.get_packet_rssi(packet.packet_hash)
                if packet.snr  == None: packet.snr  = reticulum.get_packet_snr(packet.packet_hash)
                if packet.q    == None: packet.q    = reticulum.get_packet_q(packet.packet_hash)

            if packet.rssi != None:
                self.rssi = packet.rssi
            if packet.snr != None:
                self.snr = packet.snr
            if packet.q != None:
                self.q = packet.q

    def __update_keepalive(self):
        self.keepalive = max(min(self.rtt*(Link.KEEPALIVE_MAX/Link.KEEPALIVE_MAX_RTT), Link.KEEPALIVE_MAX), Link.KEEPALIVE_MIN)
        self.stale_time = self.keepalive * Link.STALE_FACTOR
    
    def send_keepalive(self):
        keepalive_packet = RNS.Packet(self, bytes([0xFF]), context=RNS.Packet.KEEPALIVE)
        keepalive_packet.send()
        self.had_outbound(is_keepalive = True)

    def handle_request(self, request_id, unpacked_request):
        if self.status == Link.ACTIVE:
            requested_at = unpacked_request[0]
            path_hash    = unpacked_request[1]
            request_data = unpacked_request[2]

            if path_hash in self.destination.request_handlers:
                request_handler = self.destination.request_handlers[path_hash]
                path               = request_handler[0]
                response_generator = request_handler[1]
                allow              = request_handler[2]
                allowed_list       = request_handler[3]
                auto_compress      = request_handler[4]

                allowed = False
                if not allow == RNS.Destination.ALLOW_NONE:
                    if allow == RNS.Destination.ALLOW_LIST:
                        if self.__remote_identity != None and self.__remote_identity.hash in allowed_list:
                            allowed = True
                    elif allow == RNS.Destination.ALLOW_ALL:
                        allowed = True

                if allowed:
                    RNS.log("Handling request "+RNS.prettyhexrep(request_id)+" for: "+str(path), RNS.LOG_DEBUG)
                    if len(inspect.signature(response_generator).parameters) == 5:
                        response = response_generator(path, request_data, request_id, self.__remote_identity, requested_at)
                    elif len(inspect.signature(response_generator).parameters) == 6:
                        response = response_generator(path, request_data, request_id, self.link_id, self.__remote_identity, requested_at)
                    else:
                        raise TypeError("Invalid signature for response generator callback")

                    file_response = False
                    file_handle   = None
                    if type(response) == list or type(response) == tuple:
                        metadata = None
                        if len(response) > 0 and type(response[0]) == io.BufferedReader:
                            if len(response) > 1: metadata = response[1]
                            file_handle = response[0]
                            file_response = True

                    if response != None:
                        if file_response:
                            response_resource = RNS.Resource(file_handle, self, metadata=metadata, request_id = request_id, is_response = True, auto_compress=auto_compress)
                        else:
                            packed_response = umsgpack.packb([request_id, response])
                            if len(packed_response) <= self.mdu:
                                RNS.Packet(self, packed_response, RNS.Packet.DATA, context = RNS.Packet.RESPONSE).send()
                            else:
                                response_resource = RNS.Resource(packed_response, self, request_id = request_id, is_response = True, auto_compress=auto_compress)
                else:
                    identity_string = str(self.get_remote_identity()) if self.get_remote_identity() != None else "<Unknown>"
                    RNS.log("Request "+RNS.prettyhexrep(request_id)+" from "+identity_string+" not allowed for: "+str(path), RNS.LOG_DEBUG)

    def handle_response(self, request_id, response_data, response_size, response_transfer_size, metadata=None):
        if self.status == Link.ACTIVE:
            remove = None
            for pending_request in self.pending_requests:
                if pending_request.request_id == request_id:
                    remove = pending_request
                    try:
                        pending_request.response_size = response_size
                        if pending_request.response_transfer_size == None:
                            pending_request.response_transfer_size = 0
                        pending_request.response_transfer_size += response_transfer_size
                        pending_request.response_received(response_data, metadata)
                    except Exception as e:
                        RNS.log("Error occurred while handling response. The contained exception was: "+str(e), RNS.LOG_ERROR)

                    break

            if remove != None:
                if remove in self.pending_requests:
                    self.pending_requests.remove(remove)

    def request_resource_concluded(self, resource):
        if resource.status == RNS.Resource.COMPLETE:
            packed_request    = resource.data.read()
            unpacked_request  = umsgpack.unpackb(packed_request)
            request_id        = RNS.Identity.truncated_hash(packed_request)
            request_data      = unpacked_request

            self.handle_request(request_id, request_data)
        else:
            RNS.log("Incoming request resource failed with status: "+RNS.hexrep([resource.status]), RNS.LOG_DEBUG)

    def response_resource_concluded(self, resource):
        if resource.status == RNS.Resource.COMPLETE:
            # If the response resource has metadata, this
            # is a file response, and we'll pass the open
            # file handle directly.
            if resource.has_metadata:
                self.handle_response(resource.request_id, resource.data, resource.total_size, resource.size, metadata=resource.metadata)

            # If not, we'll unpack the response data and
            # pass the unpacked structure to the handler
            else:
                packed_response   = resource.data.read()
                unpacked_response = umsgpack.unpackb(packed_response)
                request_id        = unpacked_response[0]
                response_data     = unpacked_response[1]
                self.handle_response(request_id, response_data, resource.total_size, resource.size)

        else:
            RNS.log("Incoming response resource failed with status: "+RNS.hexrep([resource.status]), RNS.LOG_DEBUG)
            for pending_request in self.pending_requests:
                if pending_request.request_id == resource.request_id:
                    pending_request.request_timed_out(None)

    def get_channel(self):
        """
        Get the ``Channel`` for this link.

        :return: ``Channel`` object
        """
        if self._channel is None:
            self._channel = Channel(LinkChannelOutlet(self))
        return self._channel

    def receive(self, packet):
        self.watchdog_lock = True
        if not self.status == Link.CLOSED and not (self.initiator and packet.context == RNS.Packet.KEEPALIVE and packet.data == bytes([0xFF])):
            if packet.receiving_interface != self.attached_interface:
                RNS.log(f"Link-associated packet received on unexpected interface {packet.receiving_interface} instead of {self.attached_interface}! Someone might be trying to manipulate your communication!", RNS.LOG_ERROR)
            else:
                self.last_inbound = time.time()
                if packet.context != RNS.Packet.KEEPALIVE:
                    self.last_data = self.last_inbound
                self.rx += 1
                self.rxbytes += len(packet.data)
                if self.status == Link.STALE:
                    self.status = Link.ACTIVE

                if packet.packet_type == RNS.Packet.DATA:
                    should_query = False
                    if packet.context == RNS.Packet.NONE:
                        plaintext = self.decrypt(packet.data)
                        packet.ratchet_id = self.link_id
                        if plaintext != None:
                            self.__update_phy_stats(packet, query_shared=True)

                            if self.callbacks.packet != None:
                                thread = threading.Thread(target=self.callbacks.packet, args=(plaintext, packet))
                                thread.daemon = True
                                thread.start()
                            
                            if self.destination.proof_strategy == RNS.Destination.PROVE_ALL:
                                packet.prove()

                            elif self.destination.proof_strategy == RNS.Destination.PROVE_APP:
                                if self.destination.callbacks.proof_requested:
                                    try:
                                        if self.destination.callbacks.proof_requested(packet):
                                            packet.prove()
                                    except Exception as e:
                                        RNS.log("Error while executing proof request callback from "+str(self)+". The contained exception was: "+str(e), RNS.LOG_ERROR)

                    elif packet.context == RNS.Packet.LINKIDENTIFY:
                        plaintext = self.decrypt(packet.data)
                        if plaintext != None:
                            if not self.initiator and len(plaintext) == RNS.Identity.KEYSIZE//8 + RNS.Identity.SIGLENGTH//8:
                                public_key   = plaintext[:RNS.Identity.KEYSIZE//8]
                                signed_data  = self.link_id+public_key
                                signature    = plaintext[RNS.Identity.KEYSIZE//8:RNS.Identity.KEYSIZE//8+RNS.Identity.SIGLENGTH//8]
                                identity     = RNS.Identity(create_keys=False)
                                identity.load_public_key(public_key)

                                if identity.validate(signature, signed_data):
                                    self.__remote_identity = identity
                                    if self.callbacks.remote_identified != None:
                                        try:
                                            self.callbacks.remote_identified(self, self.__remote_identity)
                                        except Exception as e:
                                            RNS.log("Error while executing remote identified callback from "+str(self)+". The contained exception was: "+str(e), RNS.LOG_ERROR)
                                
                                    self.__update_phy_stats(packet, query_shared=True)

                    elif packet.context == RNS.Packet.REQUEST:
                        try:
                            request_id = packet.getTruncatedHash()
                            packed_request = self.decrypt(packet.data)
                            if packed_request != None:
                                unpacked_request = umsgpack.unpackb(packed_request)
                                self.handle_request(request_id, unpacked_request)
                                self.__update_phy_stats(packet, query_shared=True)
                        except Exception as e:
                            RNS.log("Error occurred while handling request. The contained exception was: "+str(e), RNS.LOG_ERROR)

                    elif packet.context == RNS.Packet.RESPONSE:
                        try:
                            packed_response = self.decrypt(packet.data)
                            if packed_response != None:
                                unpacked_response = umsgpack.unpackb(packed_response)
                                request_id = unpacked_response[0]
                                response_data = unpacked_response[1]
                                transfer_size = len(umsgpack.packb(response_data))-2
                                self.handle_response(request_id, response_data, transfer_size, transfer_size)
                                self.__update_phy_stats(packet, query_shared=True)
                        except Exception as e:
                            RNS.log("Error occurred while handling response. The contained exception was: "+str(e), RNS.LOG_ERROR)

                    elif packet.context == RNS.Packet.LRRTT:
                        if not self.initiator:
                            self.rtt_packet(packet)
                            self.__update_phy_stats(packet, query_shared=True)

                    elif packet.context == RNS.Packet.LINKCLOSE:
                        self.teardown_packet(packet)
                        self.__update_phy_stats(packet, query_shared=True)

                    elif packet.context == RNS.Packet.RESOURCE_ADV:
                        packet.plaintext = self.decrypt(packet.data)
                        if packet.plaintext != None:
                            self.__update_phy_stats(packet, query_shared=True)

                            if RNS.ResourceAdvertisement.is_request(packet):
                                RNS.Resource.accept(packet, callback=self.request_resource_concluded)
                            elif RNS.ResourceAdvertisement.is_response(packet):
                                request_id = RNS.ResourceAdvertisement.read_request_id(packet)
                                for pending_request in self.pending_requests:
                                    if pending_request.request_id == request_id:
                                        response_resource = RNS.Resource.accept(packet, callback=self.response_resource_concluded, progress_callback=pending_request.response_resource_progress, request_id = request_id)
                                        if response_resource != None:
                                            if pending_request.response_size == None:
                                                pending_request.response_size = RNS.ResourceAdvertisement.read_size(packet)
                                            if pending_request.response_transfer_size == None:
                                                pending_request.response_transfer_size = 0
                                            pending_request.response_transfer_size += RNS.ResourceAdvertisement.read_transfer_size(packet)
                                            if pending_request.started_at == None:
                                                pending_request.started_at = time.time()
                                            pending_request.response_resource_progress(response_resource)

                            elif self.resource_strategy == Link.ACCEPT_NONE:
                                pass
                            elif self.resource_strategy == Link.ACCEPT_APP:
                                if self.callbacks.resource != None:
                                    try:
                                        resource_advertisement = RNS.ResourceAdvertisement.unpack(packet.plaintext)
                                        resource_advertisement.link = self
                                        if self.callbacks.resource(resource_advertisement):
                                            RNS.Resource.accept(packet, self.callbacks.resource_concluded)
                                        else:
                                            RNS.Resource.reject(packet)
                                    except Exception as e:
                                        RNS.log("Error while executing resource accept callback from "+str(self)+". The contained exception was: "+str(e), RNS.LOG_ERROR)
                            elif self.resource_strategy == Link.ACCEPT_ALL:
                                RNS.Resource.accept(packet, self.callbacks.resource_concluded)

                    elif packet.context == RNS.Packet.RESOURCE_REQ:
                        plaintext = self.decrypt(packet.data)
                        if plaintext != None:
                            self.__update_phy_stats(packet, query_shared=True)
                            if ord(plaintext[:1]) == RNS.Resource.HASHMAP_IS_EXHAUSTED:
                                resource_hash = plaintext[1+RNS.Resource.MAPHASH_LEN:RNS.Identity.HASHLENGTH//8+1+RNS.Resource.MAPHASH_LEN]
                            else:
                                resource_hash = plaintext[1:RNS.Identity.HASHLENGTH//8+1]

                            for resource in self.outgoing_resources:
                                if resource.hash == resource_hash:
                                    # We need to check that this request has not been
                                    # received before in order to avoid sequencing errors.
                                    if not packet.packet_hash in resource.req_hashlist:
                                        resource.req_hashlist.append(packet.packet_hash)
                                        resource.request(plaintext)
                                        
                                        # TODO: Test and possibly enable this at some point
                                        # def request_job():
                                        #     resource.request(plaintext)
                                        # threading.Thread(target=request_job, daemon=True).start()

                    elif packet.context == RNS.Packet.RESOURCE_HMU:
                        plaintext = self.decrypt(packet.data)
                        if plaintext != None:
                            self.__update_phy_stats(packet, query_shared=True)
                            resource_hash = plaintext[:RNS.Identity.HASHLENGTH//8]
                            for resource in self.incoming_resources:
                                if resource_hash == resource.hash:
                                    resource.hashmap_update_packet(plaintext)

                    elif packet.context == RNS.Packet.RESOURCE_ICL:
                        plaintext = self.decrypt(packet.data)
                        if plaintext != None:
                            self.__update_phy_stats(packet)
                            resource_hash = plaintext[:RNS.Identity.HASHLENGTH//8]
                            for resource in self.incoming_resources:
                                if resource_hash == resource.hash:
                                    resource.cancel()

                    elif packet.context == RNS.Packet.RESOURCE_RCL:
                        plaintext = self.decrypt(packet.data)
                        if plaintext != None:
                            self.__update_phy_stats(packet)
                            resource_hash = plaintext[:RNS.Identity.HASHLENGTH//8]
                            for resource in self.outgoing_resources:
                                if resource_hash == resource.hash:
                                    resource._rejected()

                    elif packet.context == RNS.Packet.KEEPALIVE:
                        if not self.initiator and packet.data == bytes([0xFF]):
                            keepalive_packet = RNS.Packet(self, bytes([0xFE]), context=RNS.Packet.KEEPALIVE)
                            keepalive_packet.send()
                            self.had_outbound(is_keepalive = True)


                    # TODO: find the most efficient way to allow multiple
                    # transfers at the same time, sending resource hash on
                    # each packet is a huge overhead. Probably some kind
                    # of hash -> sequence map
                    elif packet.context == RNS.Packet.RESOURCE:
                        for resource in self.incoming_resources:
                            resource.receive_part(packet)
                            self.__update_phy_stats(packet)

                    elif packet.context == RNS.Packet.CHANNEL:
                        if not self._channel:
                            RNS.log(f"Channel data received without open channel", RNS.LOG_DEBUG)
                        else:
                            packet.prove()
                            plaintext = self.decrypt(packet.data)
                            if plaintext != None:
                                self.__update_phy_stats(packet)
                                self._channel._receive(plaintext)

                elif packet.packet_type == RNS.Packet.PROOF:
                    if packet.context == RNS.Packet.RESOURCE_PRF:
                        resource_hash = packet.data[0:RNS.Identity.HASHLENGTH//8]
                        for resource in self.outgoing_resources:
                            if resource_hash == resource.hash:
                                resource.validate_proof(packet.data)
                                self.__update_phy_stats(packet, query_shared=True)

        self.watchdog_lock = False


    def encrypt(self, plaintext):
        try:
            if not self.token:
                try: self.token = Token(self.derived_key)
                except Exception as e:
                    RNS.log("Could not instantiate token while performing encryption on link "+str(self)+". The contained exception was: "+str(e), RNS.LOG_ERROR)
                    raise e

            return self.token.encrypt(plaintext)

        except Exception as e:
            RNS.log("Encryption on link "+str(self)+" failed. The contained exception was: "+str(e), RNS.LOG_ERROR)
            raise e


    def decrypt(self, ciphertext):
        try:
            if not self.token: self.token = Token(self.derived_key)
            return self.token.decrypt(ciphertext)

        except Exception as e:
            RNS.log("Decryption failed on link "+str(self)+". The contained exception was: "+str(e), RNS.LOG_ERROR)
            return None


    def sign(self, message):
        return self.sig_prv.sign(message)

    def validate(self, signature, message):
        try:
            self.peer_sig_pub.verify(signature, message)
            return True
        except Exception as e:
            return False

    def set_link_established_callback(self, callback):
        self.callbacks.link_established = callback

    def set_link_closed_callback(self, callback):
        """
        Registers a function to be called when a link has been
        torn down.

        :param callback: A function or method with the signature *callback(link)* to be called.
        """
        self.callbacks.link_closed = callback

    def set_packet_callback(self, callback):
        """
        Registers a function to be called when a packet has been
        received over this link.

        :param callback: A function or method with the signature *callback(message, packet)* to be called.
        """
        self.callbacks.packet = callback

    def set_resource_callback(self, callback):
        """
        Registers a function to be called when a resource has been
        advertised over this link. If the function returns *True*
        the resource will be accepted. If it returns *False* it will
        be ignored.

        :param callback: A function or method with the signature *callback(resource)* to be called. Please note that only the basic information of the resource is available at this time, such as *get_transfer_size()*, *get_data_size()*, *get_parts()* and *is_compressed()*.
        """
        self.callbacks.resource = callback

    def set_resource_started_callback(self, callback):
        """
        Registers a function to be called when a resource has begun
        transferring over this link.

        :param callback: A function or method with the signature *callback(resource)* to be called.
        """
        self.callbacks.resource_started = callback

    def set_resource_concluded_callback(self, callback):
        """
        Registers a function to be called when a resource has concluded
        transferring over this link.

        :param callback: A function or method with the signature *callback(resource)* to be called.
        """
        self.callbacks.resource_concluded = callback

    def set_remote_identified_callback(self, callback):
        """
        Registers a function to be called when an initiating peer has
        identified over this link.

        :param callback: A function or method with the signature *callback(link, identity)* to be called.
        """
        self.callbacks.remote_identified = callback

    def resource_concluded(self, resource):
        concluded_at = time.time()
        if resource in self.incoming_resources:
            self.last_resource_window = resource.window
            self.last_resource_eifr = resource.eifr
            self.incoming_resources.remove(resource)
            self.expected_rate = (resource.size*8)/(max(concluded_at-resource.started_transferring, 0.0001))
        if resource in self.outgoing_resources:
            self.outgoing_resources.remove(resource)
            self.expected_rate = (resource.size*8)/(max(concluded_at-resource.started_transferring, 0.0001))

    def set_resource_strategy(self, resource_strategy):
        """
        Sets the resource strategy for the link.

        :param resource_strategy: One of ``RNS.Link.ACCEPT_NONE``, ``RNS.Link.ACCEPT_ALL`` or ``RNS.Link.ACCEPT_APP``. If ``RNS.Link.ACCEPT_APP`` is set, the `resource_callback` will be called to determine whether the resource should be accepted or not.
        :raises: *TypeError* if the resource strategy is unsupported.
        """
        if not resource_strategy in Link.resource_strategies:
            raise TypeError("Unsupported resource strategy")
        else:
            self.resource_strategy = resource_strategy

    def register_outgoing_resource(self, resource):
        self.outgoing_resources.append(resource)

    def register_incoming_resource(self, resource):
        self.incoming_resources.append(resource)

    def has_incoming_resource(self, resource):
        for incoming_resource in self.incoming_resources:
            if incoming_resource.hash == resource.hash:
                return True

        return False

    def get_last_resource_window(self):
        return self.last_resource_window

    def get_last_resource_eifr(self):
        return self.last_resource_eifr

    def cancel_outgoing_resource(self, resource):
        if resource in self.outgoing_resources:
            self.outgoing_resources.remove(resource)
        else:
            RNS.log("Attempt to cancel a non-existing outgoing resource", RNS.LOG_ERROR)

    def cancel_incoming_resource(self, resource):
        if resource in self.incoming_resources:
            self.incoming_resources.remove(resource)
        else:
            RNS.log("Attempt to cancel a non-existing incoming resource", RNS.LOG_ERROR)

    def ready_for_new_resource(self):
        if len(self.outgoing_resources) > 0:
            return False
        else:
            return True

    def __str__(self):
        return RNS.prettyhexrep(self.link_id)


class RequestReceipt():
    """
    An instance of this class is returned by the ``request`` method of ``RNS.Link``
    instances. It should never be instantiated manually. It provides methods to
    check status, response time and response data when the request concludes.
    """

    FAILED    = 0x00
    SENT      = 0x01
    DELIVERED = 0x02
    RECEIVING = 0x03
    READY     = 0x04

    def __init__(self, link, packet_receipt = None, resource = None, response_callback = None, failed_callback = None, progress_callback = None, timeout = None, request_size = None):
        self.packet_receipt = packet_receipt
        self.resource = resource
        self.started_at = None

        if self.packet_receipt != None:
            self.hash = packet_receipt.truncated_hash
            self.packet_receipt.set_timeout_callback(self.request_timed_out)
            self.started_at = time.time()

        elif self.resource != None:
            self.hash = resource.request_id
            resource.set_callback(self.request_resource_concluded)
        
        self.link                   = link
        self.request_id             = self.hash
        self.request_size           = request_size

        self.response               = None
        self.response_transfer_size = None
        self.response_size          = None
        self.metadata               = None
        self.status                 = RequestReceipt.SENT
        self.sent_at                = time.time()
        self.progress               = 0
        self.concluded_at           = None
        self.response_concluded_at  = None

        if timeout != None:
            self.timeout        = timeout
        else:
            raise ValueError("No timeout specified for request receipt")

        self.callbacks          = RequestReceiptCallbacks()
        self.callbacks.response = response_callback
        self.callbacks.failed   = failed_callback
        self.callbacks.progress = progress_callback

        self.link.pending_requests.append(self)


    def request_resource_concluded(self, resource):
        if resource.status == RNS.Resource.COMPLETE:
            RNS.log("Request "+RNS.prettyhexrep(self.request_id)+" successfully sent as resource.", RNS.LOG_DEBUG)
            if self.started_at == None:
                self.started_at = time.time()
            self.status = RequestReceipt.DELIVERED
            self.__resource_response_timeout = time.time()+self.timeout
            response_timeout_thread = threading.Thread(target=self.__response_timeout_job)
            response_timeout_thread.daemon = True
            response_timeout_thread.start()
        else:
            RNS.log("Sending request "+RNS.prettyhexrep(self.request_id)+" as resource failed with status: "+RNS.hexrep([resource.status]), RNS.LOG_DEBUG)
            self.status = RequestReceipt.FAILED
            self.concluded_at = time.time()
            self.link.pending_requests.remove(self)

            if self.callbacks.failed != None:
                try:
                    self.callbacks.failed(self)
                except Exception as e:
                    RNS.log("Error while executing request failed callback from "+str(self)+". The contained exception was: "+str(e), RNS.LOG_ERROR)


    def __response_timeout_job(self):
        while self.status == RequestReceipt.DELIVERED:
            now = time.time()
            if now > self.__resource_response_timeout:
                self.request_timed_out(None)

            time.sleep(0.1)


    def request_timed_out(self, packet_receipt):
        self.status = RequestReceipt.FAILED
        self.concluded_at = time.time()
        self.link.pending_requests.remove(self)

        if self.callbacks.failed != None:
            try:
                self.callbacks.failed(self)
            except Exception as e:
                RNS.log("Error while executing request timed out callback from "+str(self)+". The contained exception was: "+str(e), RNS.LOG_ERROR)


    def response_resource_progress(self, resource):
        if resource != None:
            if not self.status == RequestReceipt.FAILED:
                self.status = RequestReceipt.RECEIVING
                if self.packet_receipt != None:
                    if self.packet_receipt.status != RNS.PacketReceipt.DELIVERED:
                        self.packet_receipt.status = RNS.PacketReceipt.DELIVERED
                        self.packet_receipt.proved = True
                        self.packet_receipt.concluded_at = time.time()
                        if self.packet_receipt.callbacks.delivery != None:
                            self.packet_receipt.callbacks.delivery(self.packet_receipt)

                self.progress = resource.get_progress()
                
                if self.callbacks.progress != None:
                    try:
                        self.callbacks.progress(self)
                    except Exception as e:
                        RNS.log("Error while executing response progress callback from "+str(self)+". The contained exception was: "+str(e), RNS.LOG_ERROR)
            else:
                resource.cancel()

    
    def response_received(self, response, metadata=None):
        if not self.status == RequestReceipt.FAILED:
            self.progress = 1.0
            self.response = response
            self.metadata = metadata
            self.status = RequestReceipt.READY
            self.response_concluded_at = time.time()

            if self.packet_receipt != None:
                self.packet_receipt.status = RNS.PacketReceipt.DELIVERED
                self.packet_receipt.proved = True
                self.packet_receipt.concluded_at = time.time()
                if self.packet_receipt.callbacks.delivery != None:
                    self.packet_receipt.callbacks.delivery(self.packet_receipt)

            if self.callbacks.progress != None:
                try:
                    self.callbacks.progress(self)
                except Exception as e:
                    RNS.log("Error while executing response progress callback from "+str(self)+". The contained exception was: "+str(e), RNS.LOG_ERROR)

            if self.callbacks.response != None:
                try:
                    self.callbacks.response(self)
                except Exception as e:
                    RNS.log("Error while executing response received callback from "+str(self)+". The contained exception was: "+str(e), RNS.LOG_ERROR)

    def get_request_id(self):
        """
        :returns: The request ID as *bytes*.
        """
        return self.request_id

    def get_status(self):
        """
        :returns: The current status of the request, one of ``RNS.RequestReceipt.FAILED``, ``RNS.RequestReceipt.SENT``, ``RNS.RequestReceipt.DELIVERED``, ``RNS.RequestReceipt.READY``.
        """
        return self.status

    def get_progress(self):
        """
        :returns: The progress of a response being received as a *float* between 0.0 and 1.0.
        """
        return self.progress

    def get_response(self):
        """
        :returns: The response as *bytes* if it is ready, otherwise *None*.
        """
        if self.status == RequestReceipt.READY:
            return self.response
        else:
            return None

    def get_response_time(self):
        """
        :returns: The response time of the request in seconds.
        """
        if self.status == RequestReceipt.READY:
            return self.response_concluded_at - self.started_at
        else:
            return None

    def concluded(self):
        """
        :returns: True if the associated request has concluded (successfully or with a failure), otherwise False.
        """
        if self.status == RequestReceipt.READY:
            return True
        elif self.status == RequestReceipt.FAILED:
            return True
        else:
            return False



class RequestReceiptCallbacks:
    def __init__(self):
        self.response = None
        self.failed   = None
        self.progress = None
