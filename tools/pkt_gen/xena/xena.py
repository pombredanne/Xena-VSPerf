# Copyright 2016 Red Hat Inc & Xena Networks.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Contributors:
#   Rick Alongi, Red Hat Inc.
#   Amit Supugade, Red Hat Inc.
#   Dan Amzulescu, Xena Networks
#   Christian Trautman, Red Hat Inc.

"""
Xena Traffic Generator Model
"""

# VSPerf imports
from conf import settings
from core.results.results_constants import ResultsConstants
from tools.pkt_gen.trafficgen.trafficgenhelper import (
    TRAFFIC_DEFAULTS,
    merge_spec)
from tools.pkt_gen.trafficgen.trafficgen import ITrafficGenerator

# python imports
import binascii
import logging
import subprocess
import sys
import time as Time
import xml.etree.ElementTree as ET
from collections import OrderedDict

# XenaDriver
from tools.pkt_gen.xena.XenaDriver import (
    average_stats,
    line_percentage,
    XenaSocketDriver,
    XenaManager,
    )
from tools.pkt_gen.xena.xena_json import XenaJSON

# scapy imports
import scapy.layers.inet as inet


class Xena(ITrafficGenerator):
    """
    Xena Traffic generator wrapper
    """
    _traffic_defaults = TRAFFIC_DEFAULTS.copy()
    _logger = logging.getLogger(__name__)

    def __init__(self):
        self.mono_pipe = None
        self.xmanager = None
        self._params = {}
        self._xsocket = None
        self._duration = None
        self.tx_stats = None
        self.rx_stats = None

    @property
    def traffic_defaults(self):
        """Default traffic values.

        These can be expected to be constant across traffic generators,
        so no setter is provided. Changes to the structure or contents
        will likely break traffic generator implementations or tests
        respectively.
        """
        return self._traffic_defaults

    @staticmethod
    def _create_throughput_result(root):
        """
        Create the results based off the output xml file from the Xena2544.exe
        execution
        :param root: root dictionary from xml import
        :return: Results Ordered dictionary based off ResultsConstants
        """
        # get the test type from the report file
        test_type = root[0][1].get('TestType')

        if test_type == 'Throughput':
            results = OrderedDict()
            results[ResultsConstants.THROUGHPUT_RX_FPS] = int(
                root[0][1][0][1].get('PortRxPps'))
            results[ResultsConstants.THROUGHPUT_RX_MBPS] = int(
                root[0][1][0][1].get('PortRxBpsL1')) / 1000000
            results[ResultsConstants.THROUGHPUT_RX_PERCENT] = (
                100 - int(root[0][1][0].get('TotalLossRatioPcnt'))) * float(
                    root[0][1][0].get('TotalTxRatePcnt'))/100
            results[ResultsConstants.TX_RATE_FPS] = root[0][1][0].get(
                'TotalTxRateFps')
            results[ResultsConstants.TX_RATE_MBPS] = float(
                root[0][1][0].get('TotalTxRateBpsL1')) / 1000000
            results[ResultsConstants.TX_RATE_PERCENT] = root[0][1][0].get(
                'TotalTxRatePcnt')
            try:
                results[ResultsConstants.MIN_LATENCY_NS] = float(
                    root[0][1][0][0].get('MinLatency')) * 1000
            except ValueError:
                # Stats for latency returned as N/A so just post them
                results[ResultsConstants.MIN_LATENCY_NS] = root[0][1][0][0].get(
                    'MinLatency')
            try:
                results[ResultsConstants.MAX_LATENCY_NS] = float(
                    root[0][1][0][0].get('MaxLatency')) * 1000
            except ValueError:
                # Stats for latency returned as N/A so just post them
                results[ResultsConstants.MAX_LATENCY_NS] = root[0][1][0][0].get(
                    'MaxLatency')
            try:
                results[ResultsConstants.AVG_LATENCY_NS] = float(
                    root[0][1][0][0].get('AvgLatency')) * 1000
            except ValueError:
                # Stats for latency returned as N/A so just post them
                results[ResultsConstants.AVG_LATENCY_NS] = root[0][1][0][0].get(
                    'AvgLatency')
        elif test_type == 'Back2Back':
            results = OrderedDict()

            # just mimic what Ixia does and only return the b2b frame count
            results[ResultsConstants.B2B_FRAMES] = root[0][1][0][0].get(
                'TotalTxBurstFrames')
        else:
            raise NotImplementedError('Unknown test type in report file.')

        return results

    def _create_api_result(self):
        """
        Create result dictionary per trafficgen specifications. If stats are
        not available return values of 0.
        :return: ResultsConstants as dictionary
        """
        # Handle each case of statistics based on if the data is available.
        # This prevents uncaught exceptions when the stats aren't available.
        result_dict = OrderedDict()
        if self.tx_stats.data.get(self.tx_stats.pt_stream_keys[0]):
            result_dict[ResultsConstants.TX_FRAMES] = self.tx_stats.data[
                self.tx_stats.pt_stream_keys[0]]['packets']
            result_dict[ResultsConstants.TX_RATE_FPS] = self.tx_stats.data[
                self.tx_stats.pt_stream_keys[0]]['pps']
            result_dict[ResultsConstants.TX_RATE_MBPS] = self.tx_stats.data[
                self.tx_stats.pt_stream_keys[0]]['bps'] / 1000000
            result_dict[ResultsConstants.TX_BYTES] = self.tx_stats.data[
                self.tx_stats.pt_stream_keys[0]]['bytes']
            result_dict[ResultsConstants.TX_RATE_PERCENT] = line_percentage(
                self.xmanager.ports[0], self.tx_stats, self._duration,
                self._params['traffic']['l2']['framesize'])
        else:
            self._logger.error('Transmit stats not available.')
            result_dict[ResultsConstants.TX_FRAMES] = 0
            result_dict[ResultsConstants.TX_RATE_FPS] = 0
            result_dict[ResultsConstants.TX_RATE_MBPS] = 0
            result_dict[ResultsConstants.TX_BYTES] = 0
            result_dict[ResultsConstants.TX_RATE_PERCENT] = 0

        if self.rx_stats.data.get('pr_tpldstraffic'):
            result_dict[ResultsConstants.RX_FRAMES] = self.rx_stats.data[
                'pr_tpldstraffic']['0']['packets']
            result_dict[
                ResultsConstants.THROUGHPUT_RX_FPS] = self.rx_stats.data[
                    'pr_tpldstraffic']['0']['pps']
            result_dict[
                ResultsConstants.THROUGHPUT_RX_MBPS] = self.rx_stats.data[
                    'pr_tpldstraffic']['0']['bps'] / 1000000
            result_dict[ResultsConstants.RX_BYTES] = self.rx_stats.data[
                'pr_tpldstraffic']['0']['bytes']
            result_dict[
                ResultsConstants.THROUGHPUT_RX_PERCENT] = line_percentage(
                    self.xmanager.ports[1], self.rx_stats, self._duration,
                    self._params['traffic']['l2']['framesize'])
        else:
            result_dict[ResultsConstants.RX_FRAMES] = 0
            result_dict[ResultsConstants.THROUGHPUT_RX_FPS] = 0
            result_dict[ResultsConstants.THROUGHPUT_RX_MBPS] = 0
            result_dict[ResultsConstants.RX_BYTES] = 0
            result_dict[ResultsConstants.THROUGHPUT_RX_PERCENT] = 0

        if self.rx_stats.data.get('pr_tplderrors'):
            result_dict[ResultsConstants.PAYLOAD_ERR] = self.rx_stats.data[
                'pr_tplderrors']['0']['pld']
            result_dict[ResultsConstants.SEQ_ERR] = self.rx_stats.data[
                'pr_tplderrors']['0']['seq']
        else:
            result_dict[ResultsConstants.PAYLOAD_ERR] = 0
            result_dict[ResultsConstants.SEQ_ERR] = 0

        if self.rx_stats.data.get('pr_tpldlatency'):
            result_dict[ResultsConstants.MIN_LATENCY_NS] = self.rx_stats.data[
                'pr_tpldlatency']['0']['min']
            result_dict[ResultsConstants.MAX_LATENCY_NS] = self.rx_stats.data[
                'pr_tpldlatency']['0']['max']
            result_dict[ResultsConstants.AVG_LATENCY_NS] = self.rx_stats.data[
                'pr_tpldlatency']['0']['avg']
        else:
            result_dict[ResultsConstants.MIN_LATENCY_NS] = 0
            result_dict[ResultsConstants.MAX_LATENCY_NS] = 0
            result_dict[ResultsConstants.AVG_LATENCY_NS] = 0

        return result_dict

    def _build_packet_header(self, reverse=False):
        """
        Build a packet header based on traffic profile using scapy external
        libraries.
        :param reverse: Swap source and destination info when building header
        :return: packet header in hex
        """
        srcmac = self._params['traffic']['l2'][
            'srcmac'] if not reverse else self._params['traffic']['l2'][
            'dstmac']
        dstmac = self._params['traffic']['l2'][
            'dstmac'] if not reverse else self._params['traffic']['l2'][
            'srcmac']
        srcip = self._params['traffic']['l3'][
            'srcip'] if not reverse else self._params['traffic']['l3']['dstip']
        dstip = self._params['traffic']['l3'][
            'dstip'] if not reverse else self._params['traffic']['l3']['srcip']
        layer2 = inet.Ether(src=srcmac, dst=dstmac)
        layer3 = inet.IP(src=srcip, dst=dstip,
                         proto=self._params['traffic']['l3']['proto'])
        layer4 = inet.UDP(sport=self._params['traffic']['l4']['srcport'],
                          dport=self._params['traffic']['l4']['dstport'])
        if self._params['traffic']['vlan']['enabled']:
            vlan = inet.Dot1Q(vlan=self._params['traffic']['vlan']['id'],
                              prio=self._params['traffic']['vlan']['priority'],
                              id=self._params['traffic']['vlan']['cfi'])
        else:
            vlan = None
        packet = layer2/vlan/layer3/layer4 if vlan else layer2/layer3/layer4
        packet_bytes = bytes(packet)
        packet_hex = '0x' + binascii.hexlify(packet_bytes).decode('utf-8')
        return packet_hex

    def _setup_json_config(self, trials, loss_rate, testtype=None):
        """
        Create a 2bUsed json file that will be used for xena2544.exe execution.
        :param trials: Number of trials
        :param loss_rate: The acceptable loss rate as float
        :param testtype: Either '2544_b2b' or '2544_throughput' as string
        :return: None
        """
        try:
            j_file = XenaJSON('./tools/pkt_gen/xena/profiles/baseconfig.x2544')
            j_file.set_chassis_info(
                settings.getValue('TRAFFICGEN_XENA_IP'),
                settings.getValue('TRAFFICGEN_XENA_PASSWORD')
            )
            j_file.set_port(0, settings.getValue('TRAFFICGEN_XENA_MODULE1'),
                            settings.getValue('TRAFFICGEN_XENA_PORT1')
                            )
            j_file.set_port(1, settings.getValue('TRAFFICGEN_XENA_MODULE2'),
                            settings.getValue('TRAFFICGEN_XENA_PORT2')
                            )
            j_file.set_test_options(
                packet_sizes=self._params['traffic']['l2']['framesize'],
                iterations=trials, loss_rate=loss_rate,
                duration=self._duration, micro_tpld=True if self._params[
                    'traffic']['l2']['framesize'] == 64 else False)
            if testtype == '2544_throughput':
                j_file.enable_throughput_test()
            elif testtype == '2544_b2b':
                j_file.enable_back2back_test()

            j_file.set_header_layer2(
                dst_mac=self._params['traffic']['l2']['dstmac'],
                src_mac=self._params['traffic']['l2']['srcmac'])
            j_file.set_header_layer3(
                src_ip=self._params['traffic']['l3']['srcip'],
                dst_ip=self._params['traffic']['l3']['dstip'],
                protocol=self._params['traffic']['l3']['proto'])
            j_file.set_header_layer4_udp(
                source_port=self._params['traffic']['l4']['srcport'],
                destination_port=self._params['traffic']['l4']['dstport'])
            if self._params['traffic']['vlan']['enabled']:
                j_file.set_header_vlan(
                    vlan_id=self._params['traffic']['vlan']['id'],
                    id=self._params['traffic']['vlan']['cfi'],
                    prio=self._params['traffic']['vlan']['priority'])
            j_file.add_header_segments(
                flows=self._params['traffic']['multistream'],
                multistream_layer=self._params['traffic']['stream_type'])
            # set duplex mode
            if self._params['traffic']['bidir']:
                j_file.set_topology_mesh()
            else:
                j_file.set_topology_blocks()

            j_file.write_config('./tools/pkt_gen/xena/profiles/2bUsed.x2544')
        except Exception as exc:
            self._logger.exception("Error during Xena XML setup: %s", exc)
            raise

    def _start_traffic_api(self, packet_limit):
        """
        Start the Xena traffic using the socket API driver
        :param packet_limit: packet limit for stream, set to -1 for no limit
        :return: None
        """
        if not self.xmanager:
            self._xsocket = XenaSocketDriver(
                settings.getValue('TRAFFICGEN_XENA_IP'))
            self.xmanager = XenaManager(
                self._xsocket, settings.getValue('TRAFFICGEN_XENA_USER'),
                settings.getValue('TRAFFICGEN_XENA_PASSWORD'))

        if not len(self.xmanager.ports):
            self.xmanager.ports[0] = self.xmanager.add_module_port(
                settings.getValue('TRAFFICGEN_XENA_MODULE1'),
                settings.getValue('TRAFFICGEN_XENA_PORT1'))
            if not self.xmanager.ports[0].reserve_port():
                self._logger.error(
                    'Unable to reserve port 0. Please release Xena Port')
                sys.exit(1)

        if len(self.xmanager.ports) < 2:
            self.xmanager.ports[1] = self.xmanager.add_module_port(
                settings.getValue('TRAFFICGEN_XENA_MODULE2'),
                settings.getValue('TRAFFICGEN_XENA_PORT2'))
            if not self.xmanager.ports[1].reserve_port():
                self._logger.error(
                    'Unable to reserve port 1. Please release Xena Port')
                sys.exit(1)

        # Clear port configuration for a clean start
        self.xmanager.ports[0].reset_port()
        self.xmanager.ports[1].reset_port()
        self.xmanager.ports[0].clear_stats()
        self.xmanager.ports[1].clear_stats()

        s1_p0 = self.xmanager.ports[0].add_stream()
        s1_p0.set_on()
        s1_p0.set_packet_limit(packet_limit)

        s1_p0.set_rate_fraction(10000 * self._params['traffic']['frame_rate'])
        s1_p0.set_packet_header(self._build_packet_header())
        s1_p0.set_header_protocol(
            'ETHERNET VLAN IP UDP' if self._params['traffic']['vlan'][
                'enabled'] else 'ETHERNET IP UDP')
        s1_p0.set_packet_length(
            'fixed', self._params['traffic']['l2']['framesize'], 16383)
        s1_p0.set_packet_payload('incrementing', '0x00')
        s1_p0.set_payload_id(0)

        self.xmanager.ports[0].set_port_time_limit(self._duration * 1000000)

        if self._params['traffic']['l2']['framesize'] == 64:
            # set micro tpld
            self.xmanager.ports[0].micro_tpld_enable()

        if self._params['traffic']['multistream']:
            s1_p0.enable_multistream(
                flows=self._params['traffic']['multistream'],
                layer=self._params['traffic']['stream_type'])

        if self._params['traffic']['bidir']:
            s1_p1 = self.xmanager.ports[1].add_stream()
            s1_p1.set_on()
            s1_p1.set_packet_limit(packet_limit)

            s1_p1.set_rate_fraction(10000 * self._params['traffic'][
                'frame_rate'])
            s1_p1.set_packet_header(self._build_packet_header(reverse=True))
            s1_p1.set_header_protocol(
                'ETHERNET VLAN IP UDP' if self._params['traffic']['vlan'][
                    'enabled'] else 'ETHERNET IP UDP')
            s1_p1.set_packet_length(
                'fixed', self._params['traffic']['l2']['framesize'], 16383)
            s1_p1.set_packet_payload('incrementing', '0x00')
            s1_p1.set_payload_id(1)

            self.xmanager.ports[1].set_port_time_limit(self._duration * 1000000)

            if self._params['traffic']['l2']['framesize'] == 64:
                # set micro tpld
                self.xmanager.ports[1].micro_tpld_enable()

            if self._params['traffic']['multistream']:
                s1_p1.enable_multistream(
                    flows=self._params['traffic']['multistream'],
                    layer=self._params['traffic']['stream_type'])

        if not self.xmanager.ports[0].traffic_on():
            self._logger.error(
                "Failure to start port 0. Check settings and retry.")
        if self._params['traffic']['bidir']:
            if not self.xmanager.ports[1].traffic_on():
                self._logger.error(
                    "Failure to start port 1. Check settings and retry.")
        Time.sleep(self._duration)
        # getting results
        if self._params['traffic']['bidir']:
            # need to average out both ports and assign that data
            self.rx_stats = self.xmanager.ports[1].get_rx_stats()
            self.tx_stats = self.xmanager.ports[0].get_tx_stats()
            self.tx_stats.data = average_stats(
                self.tx_stats.data,
                self.xmanager.ports[1].get_tx_stats().data)
            self.rx_stats.data = average_stats(
                self.rx_stats.data,
                self.xmanager.ports[0].get_rx_stats().data)
        else:
            # no need to average, just grab the appropriate port stats
            self.tx_stats = self.xmanager.ports[0].get_tx_stats()
            self.rx_stats = self.xmanager.ports[1].get_rx_stats()
        Time.sleep(1)

    def _stop_api_traffic(self):
        """
        Stop traffic through the socket API
        :return: Return results from _create_api_result method
        """
        self.xmanager.ports[0].traffic_off()
        if self._params['traffic']['bidir']:
            self.xmanager.ports[1].traffic_off()
        Time.sleep(5)

        stat = self._create_api_result()
        self.disconnect()
        return stat

    def connect(self):
        self._logger.debug('Connect')
        return self

    def disconnect(self):
        """Disconnect from the traffic generator.

        As with :func:`connect`, this function is optional.


        Where implemented, this function should raise an exception on
        failure.

        :returns: None
        """
        self._logger.debug('disconnect')
        if self.xmanager:
            self.xmanager.disconnect()
            self.xmanager = None

        if self._xsocket:
            self._xsocket.disconnect()
            self._xsocket = None

    def send_burst_traffic(self, traffic=None, numpkts=100, duration=20):
        """Send a burst of traffic.

        Send a ``numpkts`` packets of traffic, using ``traffic``
        configuration, with a timeout of ``time``.

        Attributes:
        :param traffic: Detailed "traffic" spec, i.e. IP address, VLAN tags
        :param numpkts: Number of packets to send
        :param duration: Time to wait to receive packets

        :returns: dictionary of strings with following data:
            - List of Tx Frames,
            - List of Rx Frames,
            - List of Tx Bytes,
            - List of List of Rx Bytes,
            - Payload Errors and Sequence Errors.
        """
        self._duration = duration

        self._params.clear()
        self._params['traffic'] = self.traffic_defaults.copy()
        if traffic:
            self._params['traffic'] = merge_spec(self._params['traffic'],
                                                 traffic)

        self._start_traffic_api(numpkts)
        return self._stop_api_traffic()

    def send_cont_traffic(self, traffic=None, duration=20):
        """Send a continuous flow of traffic.r

        Send packets at ``framerate``, using ``traffic`` configuration,
        until timeout ``time`` occurs.

        :param traffic: Detailed "traffic" spec, i.e. IP address, VLAN tags
        :param duration: Time to wait to receive packets (secs)
        :returns: dictionary of strings with following data:
            - Tx Throughput (fps),
            - Rx Throughput (fps),
            - Tx Throughput (mbps),
            - Rx Throughput (mbps),
            - Tx Throughput (% linerate),
            - Rx Throughput (% linerate),
            - Min Latency (ns),
            - Max Latency (ns),
            - Avg Latency (ns)
        """
        self._duration = duration

        self._params.clear()
        self._params['traffic'] = self.traffic_defaults.copy()
        if traffic:
            self._params['traffic'] = merge_spec(self._params['traffic'],
                                                 traffic)

        self._start_traffic_api(-1)
        return self._stop_api_traffic()

    def start_cont_traffic(self, traffic=None, duration=20):
        """Non-blocking version of 'send_cont_traffic'.

        Start transmission and immediately return. Do not wait for
        results.
        :param traffic: Detailed "traffic" spec, i.e. IP address, VLAN tags
        :param duration: Time to wait to receive packets (secs)
        """
        self._duration = duration

        self._params.clear()
        self._params['traffic'] = self.traffic_defaults.copy()
        if traffic:
            self._params['traffic'] = merge_spec(self._params['traffic'],
                                                 traffic)

        self._start_traffic_api(-1)

    def stop_cont_traffic(self):
        """Stop continuous transmission and return results.
        """
        return self._stop_api_traffic()

    def send_rfc2544_throughput(self, traffic=None, trials=3, duration=20,
                                lossrate=0.0):
        """Send traffic per RFC2544 throughput test specifications.

        See ITrafficGenerator for description
        """
        self._duration = duration

        self._params.clear()
        self._params['traffic'] = self.traffic_defaults.copy()
        if traffic:
            self._params['traffic'] = merge_spec(self._params['traffic'],
                                                 traffic)

        self._setup_json_config(trials, lossrate, '2544_throughput')

        args = ["mono", "./tools/pkt_gen/xena/Xena2544.exe", "-c",
                "./tools/pkt_gen/xena/profiles/2bUsed.x2544", "-e", "-r",
                "./tools/pkt_gen/xena", "-u",
                settings.getValue('TRAFFICGEN_XENA_USER')]
        self.mono_pipe = subprocess.Popen(args, stdout=sys.stdout)
        self.mono_pipe.communicate()
        root = ET.parse(r'./tools/pkt_gen/xena/xena2544-report.xml').getroot()
        return Xena._create_throughput_result(root)

    def start_rfc2544_throughput(self, traffic=None, trials=3, duration=20,
                                 lossrate=0.0):
        """Non-blocking version of 'send_rfc2544_throughput'.

        See ITrafficGenerator for description
        """
        self._duration = duration
        self._params.clear()
        self._params['traffic'] = self.traffic_defaults.copy()
        if traffic:
            self._params['traffic'] = merge_spec(self._params['traffic'],
                                                 traffic)

        self._setup_json_config(trials, lossrate, '2544_throughput')

        args = ["mono", "./tools/pkt_gen/xena/Xena2544.exe", "-c",
                "./tools/pkt_gen/xena/profiles/2bUsed.x2544", "-e", "-r",
                "./tools/pkt_gen/xena", "-u",
                settings.getValue('TRAFFICGEN_XENA_USER')]
        self.mono_pipe = subprocess.Popen(args, stdout=sys.stdout)

    def wait_rfc2544_throughput(self):
        """Wait for and return results of RFC2544 test.

        See ITrafficGenerator for description
        """
        self.mono_pipe.communicate()
        Time.sleep(2)
        root = ET.parse(r'./tools/pkt_gen/xena/xena2544-report.xml').getroot()
        return Xena._create_throughput_result(root)

    def send_rfc2544_back2back(self, traffic=None, trials=1, duration=20,
                               lossrate=0.0):
        """Send traffic per RFC2544 back2back test specifications.

        See ITrafficGenerator for description
        """
        self._duration = duration

        self._params.clear()
        self._params['traffic'] = self.traffic_defaults.copy()
        if traffic:
            self._params['traffic'] = merge_spec(self._params['traffic'],
                                                 traffic)

        self._setup_json_config(trials, lossrate, '2544_b2b')

        args = ["mono", "./tools/pkt_gen/xena/Xena2544.exe", "-c",
                "./tools/pkt_gen/xena/profiles/2bUsed.x2544", "-e", "-r",
                "./tools/pkt_gen/xena", "-u",
                settings.getValue('TRAFFICGEN_XENA_USER')]
        self.mono_pipe = subprocess.Popen(
            args, stdout=sys.stdout)
        self.mono_pipe.communicate()
        root = ET.parse(r'./tools/pkt_gen/xena/xena2544-report.xml').getroot()
        return Xena._create_throughput_result(root)

    def start_rfc2544_back2back(self, traffic=None, trials=1, duration=20,
                                lossrate=0.0):
        """Non-blocking version of 'send_rfc2544_back2back'.

        See ITrafficGenerator for description
        """
        self._duration = duration

        self._params.clear()
        self._params['traffic'] = self.traffic_defaults.copy()
        if traffic:
            self._params['traffic'] = merge_spec(self._params['traffic'],
                                                 traffic)

        self._setup_json_config(trials, lossrate, '2544_b2b')

        args = ["mono", "./tools/pkt_gen/xena/Xena2544.exe", "-c",
                "./tools/pkt_gen/xena/profiles/2bUsed.x2544", "-e", "-r",
                "./tools/pkt_gen/xena", "-u",
                settings.getValue('TRAFFICGEN_XENA_USER')]
        self.mono_pipe = subprocess.Popen(
            args, stdout=sys.stdout)

    def wait_rfc2544_back2back(self):
        """Wait and set results of RFC2544 test.
        """
        self.mono_pipe.communicate()
        Time.sleep(2)
        root = ET.parse(r'./tools/pkt_gen/xena/xena2544-report.xml').getroot()
        return Xena._create_throughput_result(root)


if __name__ == "__main__":
    pass

