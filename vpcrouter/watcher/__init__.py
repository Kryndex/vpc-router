"""
Copyright 2017 Pani Networks Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

"""

#
# Functions for watching route spec in daemon mode
#

import itertools
import logging
import Queue
import time

from vpcrouter              import vpc
from vpcrouter.currentstate import CURRENT_STATE


def _read_last_msg_from_queue(q):
    """
    Read all messages from a queue and return the last one.

    This is useful in our case, since all messages are always the complete
    state of things. Therefore, intermittent messages can be ignored.

    Doesn't block, returns None if there is no message waiting in the queue.

    """
    msg = None
    while True:
        try:
            # The list of IPs is always a full list.
            msg = q.get_nowait()
            q.task_done()
        except Queue.Empty:
            # No more messages, all done for now
            return msg


def _update_health_monitor_with_new_ips(route_spec, all_ips,
                                        q_monitor_ips):
    """
    Take the current route spec and compare to the current list of known IP
    addresses. If the route spec mentiones a different set of IPs, update the
    monitoring thread with that new list.

    Return the current set of IPs mentioned in the route spec.

    """
    # Extract all the IP addresses from the route spec, unique and sorted.
    new_all_ips = \
        sorted(set(itertools.chain.from_iterable(route_spec.values())))
    if new_all_ips != all_ips:
        logging.debug("New route spec detected. Updating "
                      "health-monitor with: %s" %
                      ",".join(new_all_ips))
        # Looks like we have a new list of IPs
        all_ips = new_all_ips
        q_monitor_ips.put(all_ips)
    else:
        logging.debug("New route spec detected. No changes in "
                      "IP address list, not sending update to "
                      "health-monitor.")

    return all_ips


def _event_monitor_loop(region_name, vpc_id,
                        watcher_plugin, health_plugin,
                        iterations, sleep_time):
    """
    Monitor queues to receive updates about new route specs or any detected
    failed IPs.

    If any of those have updates, notify the health-monitor thread with a
    message on a special queue and also re-process the entire routing table.

    The 'iterations' argument allows us to limit the running time of the watch
    loop for test purposes. Not used during normal operation. Also, for faster
    tests, sleep_time can be set to values less than 1.

    """
    q_route_spec                = watcher_plugin.get_route_spec_queue()
    q_monitor_ips, q_failed_ips = health_plugin.get_queues()
    time.sleep(sleep_time)   # Wait to allow monitor to report results

    current_route_spec = {}  # The last route spec we have seen
    all_ips = []             # Cache of IP addresses we currently know about
    while True:
        try:
            # Get the latest messages from the route-spec monitor and the
            # health-check monitor. At system start the route-spec queue should
            # immediately have been initialized with a first message.
            failed_ips     = _read_last_msg_from_queue(q_failed_ips)
            new_route_spec = _read_last_msg_from_queue(q_route_spec)

            if failed_ips:
                # Store the failed IPs in the shared state
                CURRENT_STATE['failed_ips'] = failed_ips

            if new_route_spec:
                # Store the new route spec in the shared state
                CURRENT_STATE['route_spec'] = new_route_spec
                current_route_spec = new_route_spec
                # Need to communicate a new set of IPs to the health
                # monitoring thread, in case the list changed. The list of
                # addresses is extracted from the route spec. Pass in the old
                # version of the address list, so that this function can
                # compare to see if there are any changes to the host list.
                all_ips = _update_health_monitor_with_new_ips(new_route_spec,
                                                              all_ips,
                                                              q_monitor_ips)

            # Spec or list of failed IPs changed? Update routes...
            # We pass in the last route spec we have seen, since we are also
            # here in case we only have failed IPs, but no new route spec.
            if new_route_spec or failed_ips:
                vpc.handle_spec(region_name, vpc_id, current_route_spec,
                                failed_ips if failed_ips else [])

            # If iterations are provided, count down and exit
            if iterations is not None:
                iterations -= 1
                if iterations == 0:
                    break

            time.sleep(sleep_time)
        except KeyboardInterrupt:
            # Allow exit via keyboard interrupt, useful during development
            return
        except Exception as e:
            # Of course we should never get here, but if we do, better to log
            # it and keep operating best we can...
            logging.error("*** Uncaught exception 1: %s" % str(e))


def start_plugins(conf, watcher_plugin_class, health_plugin_class,
                  sleep_time):
    """
    Start the working threads:

    - Health monitor (the health plugin)
    - Config change monitor (the watcher plugin)

    """
    # No matter what the chosen plugin to watch for config updates: We get a
    # plugin-handle back. This gives us a start(), stop() and
    # get_route_spec_queue() function. All watcher plugins provide the same
    # interface.
    watcher_plugin = watcher_plugin_class(conf)
    watcher_plugin.start()

    # Similarly for the health-monitor-plugin. It gives us a get_queues()
    # function, to get the monitor-ips and failed-ips queues.
    health_plugin = health_plugin_class(conf)
    health_plugin.start()

    return watcher_plugin, health_plugin


def stop_plugins(watcher_plugin, health_plugin):
    """
    Stops all plugins.

    """
    logging.debug("Stopping health-check monitor...")
    health_plugin.stop()

    logging.debug("Stopping config change observer...")
    watcher_plugin.stop()


def start_watcher(conf, watcher_plugin_class, health_plugin_class,
                  iterations=None, sleep_time=1):
    """
    Start watcher loop, listening for config changes or failed hosts.

    Also starts the various service threads.

    VPC router watches for any changes in the config and updates/adds/deletes
    routes as necessary. If failed hosts are reported, routes are also updated
    as needed.

    This function starts a few working threads:

    - The watcher plugin to monitor for updated route specs.
    - A health monitor plugin for instances mentioned in the route spec.

    It then drops into a loop to receive messages from the health monitoring
    thread and watcher plugin and re-process the config if any failed IPs are
    reported.

    The loop itself is in its own function to facilitate easier testing.

    """
    # Start the working threads (health monitor, config event monitor, etc.)
    # and return the thread handles and message queues in a thread-info dict.
    watcher_plugin, health_plugin = \
            start_plugins(conf, watcher_plugin_class, health_plugin_class,
                           sleep_time)

    # Start the loop to process messages from the monitoring
    # threads about any failed IP addresses or updated route specs.
    _event_monitor_loop(conf['region_name'], conf['vpc_id'],
                        watcher_plugin, health_plugin,
                        iterations, sleep_time)

    # Stopping plugins and collecting all worker threads when we are done
    stop_plugins(watcher_plugin, health_plugin)
