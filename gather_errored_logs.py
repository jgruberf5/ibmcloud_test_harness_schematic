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
import os
import sys
import json
import logging
import datetime
import time
import requests
import concurrent.futures
import threading
import shutil
import random
import subprocess

LOG = logging.getLogger('ibmcloud_test_harness_get_errored_logs')
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
ERRORED_TEST_IDS = []

CONFIG_FILE = "%s/runners-config.json" % SCRIPT_DIR
CONFIG = {}

DISCOVERED_FLOATING_IPS = {}

AUTH_ENDPOINT = 'https://iam.cloud.ibm.com/identity/token'

SESSION_TOKEN = None
REFRESH_TOKEN = None
SESSION_TIMESTAMP = 0

REQUEST_RETRIES = 10
REQUEST_DELAY = 10

WORKSPACE_DELETE_TIMEOUT = 300
WORKSPACE_RETRY_INTERVAL = 10
WORKSPACE_STATUS_POLL_INTERVAL = 10

MY_PID = None


def check_pid(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    else:
        return True


def get_iam_token():
    global SESSION_TOKEN, REFRESH_TOKEN, SESSION_TIMESTAMP
    if SESSION_TIMESTAMP > 0 and ((SESSION_TIMESTAMP - int(time.time())) > 60):
        return SESSION_TOKEN
    headers = {
        "Accept": "application/json",
        "Authorization": "Basic Yng6Yng=",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = "apikey=%s&grant_type=urn:ibm:params:oauth:grant-type:apikey" % CONFIG['api_key']
    try:
        response = requests.post(AUTH_ENDPOINT, headers=headers, data=data)
        if response.status_code < 300:
            response_json = response.json()
            SESSION_TIMESTAMP = (int(time.time()) +
                                 int(response_json['expires_in']))
            SESSION_TOKEN = response_json['access_token']
            REFRESH_TOKEN = response_json['refresh_token']
            return SESSION_TOKEN
        else:
            LOG.error('could not get an access token %d - %s',
                      response.status_code, response.content)
            SESSION_TIMESTAMP = 0
            return None
    except Exception as te:
        LOG.error('exception while getting IAM token %s - %s',
                  te, response.status_code)
        SESSION_TIMESTAMP = 0
        return None


def get_refresh_token():
    now = int(time.time())
    if SESSION_TIMESTAMP > 0 and ((SESSION_TIMESTAMP - int(time.time())) > 60):
        return REFRESH_TOKEN
    else:
        get_iam_token()
        return REFRESH_TOKEN


def get_floating_ip(instance_name):
    global DISCOVERED_FLOATING_IPS
    if instance_name in DISCOVERED_FLOATING_IPS:
        return DISCOVERED_FLOATING_IPS[instance_name]
    else:
        regions = ['us-south', 'us-east', 'eu-gb', 'eu-de', 'jp-tok', 'au-syd']
        for region in regions:
            url = "%s.iaas.cloud.ibm.com/v1/instances?version=2020-09-08&generation=2" % region
            token = get_iam_token()
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % token
            }
            response = requests.get(url, headers=headers)
            response_json = response.json()
            for instance in response_json['instances']:
                DISCOVERED_FLOATING_IPS[instance['name']] = None
                instance_id = instance['id']
                LOG.info('getting network interfaces for %s' % instance['name'])
                nint_url = "%s.iaas.cloud.ibm.com/v1/instances/%s/network_interfaces?version=2020-09-08&generation=2" % (
                    region, instance_id)
                nint_response = requests.get(nint_url, headers=headers)
                nint_response_json = nint_response.json()
                for nint in nint_response_json['network_interfaces']:
                    if 'floating_ips' in nint and len(nint['floating_ips']) > 0:
                        LOG.info('found floating IP %s for instance %s', nint['floating_ips'][0]['address'], instance['name'])
                        DISCOVERED_FLOATING_IPS[instance['name']
                                                ] = nint['floating_ips'][0]['address']
        if instance_name in DISCOVERED_FLOATING_IPS:
            return DISCOVERED_FLOATING_IPS[instance_name]
    return None


def get_logs(test_dir):
    instance_name = "t-%s" % os.path.basename(test_dir)
    ip_address = get_floating_ip(instance_name)
    LOG.info('instance %s is at %s', instance_name, ip_address)
    if ip_address:
        cmd = "scp root@%s:/var/log/restjava* ./" % ip_address
        LOG.info('running cmd %s', cmd)
        subprocess.call(cmd, cwd=test_dir, shell=True)
        cmd = "scp root@%s:/var/log/restnoded/* ./" % ip_address
        LOG.info('running cmd %s', cmd)
        subprocess.call(cmd, cwd=test_dir, shell=True)


def build_pool():
    pool = []
    for rt in os.listdir(ERRORED_DIR):
        if ERRORED_TEST_IDS:
            for id in ERRORED_TEST_IDS:
                if id.strip() == rt:
                    pool.append(os.path.join(ERRORED_DIR, rt))
        else:
            pool.append(os.path.join(ERRORED_DIR, rt))
    return pool


def runner():
    test_pool = build_pool()
    random.shuffle(test_pool)
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONFIG['thread_pool_size']) as executor:
        for test_path in test_pool:
            executor.submit(get_logs, test_path)


def initialize():
    global MY_PID, CONFIG, ERRORED_TEST_IDS, WORKSPACE_DELETE_TIMEOUT, \
        WORKSPACE_RETRY_INTERVAL, WORKSPACE_STATUS_POLL_INTERVAL
    MY_PID = os.getpid()
    os.makedirs(QUEUE_DIR, exist_ok=True)
    os.makedirs(ERRORED_DIR, exist_ok=True)
    os.makedirs(COMPLETE_DIR, exist_ok=True)
    filter_ids = os.getenv('ERRORED_TEST_IDS', "")
    if filter_ids:
        ERRORED_TEST_IDS = filter_ids.split(',')
    LOG.debug('test filter is: %s', ERRORED_TEST_IDS)
    config_json = ''
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
    runner()
    ERROR_MESSAGE = ''
    ERROR = False

    STOP_TIME = time.time()
    DURATION = STOP_TIME - START_TIME
    LOG.debug(
        'process end time: %s - ran %s (seconds)',
        datetime.datetime.fromtimestamp(
            STOP_TIME).strftime("%A, %B %d, %Y %I:%M:%S"),
        DURATION
    )
