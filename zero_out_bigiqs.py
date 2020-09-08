#!/usr/bin/env python3

# coding=utf-8
# pylint: disable=broad-except,unused-argument,line-too-long, unused-variable
# Copyright (c) 2016-2018, F5 Networks, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import logging
import datetime
import requests
import os
import sys
import json
import time


LOG = logging.getLogger('ibmcloud_test_harness_zero_bigiq')
LOG.setLevel(logging.DEBUG)
FORMATTER = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
LOGSTREAM = logging.StreamHandler(sys.stdout)
LOGSTREAM.setFormatter(FORMATTER)
LOG.addHandler(LOGSTREAM)

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

QUEUE_DIR = "%s/queued_tests" % SCRIPT_DIR
RUNNING_DIR = "%s/running_tests" % SCRIPT_DIR
COMPLETE_DIR = "%s/completed_tests" % SCRIPT_DIR
ERRORED_DIR = "%s/errored_tests" % SCRIPT_DIR

CONFIG_FILE = "%s/builder-config.json" % SCRIPT_DIR
CONFIG = {}

MY_PID = None


def get_bigiq_session(host, username, password, timeout):
    if requests.__version__ < '2.9.1':
        requests.packages.urllib3.disable_warnings()  # pylint: disable=no-member
    bigiq = requests.Session()
    bigiq.verify = False
    bigiq.headers.update({'Content-Type': 'application/json'})
    bigiq.timeout = timeout
    token_auth_body = {
        'username': username,
        'password': password,
        'loginProviderName': 'local'
    }
    login_url = "https://%s/mgmt/shared/authn/login" % host
    response = bigiq.post(login_url,
                          json=token_auth_body,
                          verify=False,
                          auth=requests.auth.HTTPBasicAuth(username, password))
    response_json = response.json()
    bigiq.headers.update(
        {'X-F5-Auth-Token': response_json['token']['token']})
    bigiq.base_url = 'https://%s/mgmt/cm/device/licensing/pool' % host
    return bigiq


def get_pool_id(bigiq_session, pool_name):
    pools_url = '%s/utility/licenses?$select=regKey,kind,name,unitsOfMeasure' % \
        bigiq_session.base_url
    response = bigiq_session.get(pools_url)
    response.raise_for_status()
    response_json = response.json()
    pools = response_json['items']
    for pool in pools:
        if pool['name'] == pool_name or pool['regKey'] == pool_name:
            return pool['regKey']
    return None


def get_offerings(bigiq_session, pool_id, search1, search2):
    pools_url = '%s/utility/licenses' % bigiq_session.base_url
    offerings_url = '%s/%s/offerings?$select=id,kind,name' % (
        pools_url, pool_id)
    response = bigiq_session.get(offerings_url)
    response.raise_for_status()
    response_json = response.json()
    offerings = response_json['items']
    for offering in offerings:
        if offering['name'].find(search1) >= 0:
            if search2:
                if offering['name'].find(search2) >= 0:
                    return offering['id']
            else:
                return offering['id']
    return None


def delete_all_grants(bigiq_session, pool_id, offering_id):
    pools_url = '%s/utility/licenses' % bigiq_session.base_url
    offerings_url = '%s/%s/offerings' % (pools_url, pool_id)
    members_url = '%s/%s/members' % (offerings_url, offering_id)
    response = bigiq_session.get(members_url)
    if response.status_code != 404:
        response.raise_for_status()
        response_json = response.json()
        members = response_json['items']
        for member in members:
            LOG.info('deleting license grant %s', member['id'])
            member_url = '%s/%s' % (members_url, member['id'])
            response = bigiq_session.delete(member_url)
            response.raise_for_status()


def clean():
    if 'zone_license_hosts' in CONFIG:
        zones = CONFIG['zone_license_hosts']
        for zone in zones.keys():
            bigiq_config = CONFIG['zone_license_hosts'][zone]
            bigiq = get_bigiq_session(
                bigiq_config['license_host'], bigiq_config['license_username'], bigiq_config['license_password'], 30)
            pool_id = get_pool_id(bigiq, bigiq_config['license_pool'])
            if pool_id:
                offering_id = get_offerings(
                    bigiq, pool_id, bigiq_config['license_sku_keyword_1'], bigiq_config['license_sku_keyword_2'])
                if offering_id:
                    delete_all_grants(bigiq, pool_id, offering_id)


def initialize():
    global MY_PID, CONFIG
    MY_PID = os.getpid()
    with open(CONFIG_FILE, 'r') as cf:
        config_json = cf.read()
    config = json.loads(config_json)
    # intialize missing config defaults
    CONFIG = config


if __name__ == "__main__":
    START_TIME = time.time()
    LOG.debug('process start time: %s', datetime.datetime.fromtimestamp(
        START_TIME).strftime("%A, %B %d, %Y %I:%M:%S"))
    initialize()
    ERROR_MESSAGE = ''
    ERROR = False
    KEEP_CLEAN = os.getenv('KEEP_CLEAN', '0')
    if KEEP_CLEAN == '1' or KEEP_CLEAN.lower() == 'true':
        while True:
            try:
                clean()
                time.sleep(300)
            except Exception as ex:
                LOG.error('exception releasing grants: %s', ex)
    else:
        clean()
    STOP_TIME = time.time()
    DURATION = STOP_TIME - START_TIME
    LOG.debug(
        'process end time: %s - ran %s (seconds)',
        datetime.datetime.fromtimestamp(
            STOP_TIME).strftime("%A, %B %d, %Y %I:%M:%S"),
        DURATION
    )
