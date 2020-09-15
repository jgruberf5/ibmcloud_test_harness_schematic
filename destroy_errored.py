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

LOG = logging.getLogger('ibmcloud_test_harness_destroy_errored')
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

KNOWN_WORKSPACES = {}

AUTH_ENDPOINT = 'https://iam.cloud.ibm.com/identity/token'

SESSION_TOKEN = None
REFRESH_TOKEN = None
SESSION_TIMESTAMP = 0
SESSION_SECONDS = 1800
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
    now = int(time.time())
    if SESSION_TIMESTAMP > 0 and ((now - SESSION_TIMESTAMP) < SESSION_SECONDS):
        return SESSION_TOKEN
    headers = {
        "Accept": "application/json",
        "Authorization": "Basic Yng6Yng=",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = "apikey=%s&grant_type=urn:ibm:params:oauth:grant-type:apikey" % CONFIG['api_key']
    response = requests.post(AUTH_ENDPOINT, headers=headers, data=data)
    if response.status_code < 300:
        SESSION_TIMESTAMP = int(time.time())
        response_json = response.json()
        SESSION_TOKEN = response_json['access_token']
        REFRESH_TOKEN = response_json['refresh_token']
        return SESSION_TOKEN
    else:
        LOG.error('could not get an access token %d - %s',
                  response.status_code, response.content)
        return None


def get_refresh_token():
    now = int(time.time())
    if SESSION_TIMESTAMP > 0 and ((now - SESSION_TIMESTAMP) < SESSION_SECONDS):
        return REFRESH_TOKEN
    else:
        get_iam_token()
        return REFRESH_TOKEN


def get_workspace_id(url, test_id, timeout):
    global KNOWN_WORKSPACES
    if test_id in KNOWN_WORKSPACES:
        return KNOWN_WORKSPACES[test_id]
    else:
        end_time = time.time() + timeout
        while (end_time - time.time()) > 0:
            try:
                token = get_iam_token()
                refresh_token = get_refresh_token()
                headers = {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": "Bearer %s" % token,
                    "refresh_token": refresh_token
                }
                response = requests.get(url, headers=headers)
                while response.status_code == 429:
                    LOG.debug('exceeded throttling limit.. retrying')
                    time.sleep(WORKSPACE_RETRY_INTERVAL)
                    response = requests.post(url, headers=headers)
                    LOG.info('getting workspace id returned %d for %s',
                             response.status_code, url)
                if response.status_code < 400:
                    response_json = response.json()
                    for ws in response_json['workspaces']:
                        if ws['name'].endswith(test_id):
                            KNOWN_WORKSPACES[test_id] = ws['id']
                            return ws['id']
                    LOG.error(
                        'no workspace found for test: %s, deleting', test_id)
                    return False
            except Exception as pe:
                LOG.error(
                    'exception getting workspace id for %s - %s', test_id, pe)
                return False
            time.sleep(WORKSPACE_STATUS_POLL_INTERVAL)


def poll_workspace_until(url, statuses, timeout):
    w_id = os.path.basename(url)
    LOG.debug('polling workspace %s for %d seconds', w_id, timeout)
    end_time = time.time() + timeout
    while (end_time - time.time()) > 0:
        try:
            token = get_iam_token()
            refresh_token = get_refresh_token()
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % token,
                "refresh_token": refresh_token
            }
            response = requests.get(url, headers=headers)
            while response.status_code == 429:
                LOG.debug('exceeded throttling limit.. retrying')
                time.sleep(WORKSPACE_RETRY_INTERVAL)
                response = requests.post(url, headers=headers)
                LOG.info('polling workspace returned %d for %s',
                         response.status_code, url)
            if response.status_code < 400:
                response_json = response.json()
                if response_json['status'].lower() in statuses:
                    LOG.info('polling workspace %s return status %s',
                             w_id, response_json['status'])
                    return response_json['status']
                else:
                    LOG.debug('polling workspace %s interim status %s',
                              w_id, response_json['status'])

        except Exception as pe:
            LOG.error('exception polling workspace %s - %s', w_id, pe)
            return False
        time.sleep(WORKSPACE_STATUS_POLL_INTERVAL)
    return False


def delete_workspace(test_dir):
    url = None
    with open(os.path.join(test_dir, 'service_endpoint.url'), 'r') as eu:
        url = eu.read()
    test_id = os.path.basename(test_dir)
    workspace_id = get_workspace_id(url, test_id)
    if not workspace_id:
        shutil.rmtree(test_dir)
    LOG.info('deleting Schematic workspace for %s', workspace_id)
    status_url = "%s/%s" % (url, workspace_id)
    status_returned = poll_workspace_until(
        status_url, ['inactive', 'active', 'failed'], WORKSPACE_DELETE_TIMEOUT)
    delete_url = "%s/%s" % (url, workspace_id)
    if status_returned.lower() == 'active':
        delete_url = "%s/%s/?destroyResources=true" % (url, workspace_id)
    token = get_iam_token()
    refresh_token = get_refresh_token()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % token,
        "refresh_token": refresh_token
    }
    response = requests.delete(delete_url, headers=headers)
    LOG.info('workspace delete returned %d for %s',
             response.status_code, workspace_id)
    while response.status_code == 409:
        LOG.debug('workspace locked.. retrying')
        time.sleep(WORKSPACE_RETRY_INTERVAL)
        response = requests.delete(delete_url, headers=headers)
        LOG.info('workspace delete returned %d for %s',
                 response.status_code, workspace_id)
    while response.status_code == 429:
        LOG.debug('exceeded throttling limit.. retrying')
        time.sleep(WORKSPACE_RETRY_INTERVAL)
        response = requests.post(delete_url, headers=headers)
        LOG.info('workspace delete returned %d for %s',
                 response.status_code, delete_url)
    if response.status_code < 300:
        shutil.rmtree(test_dir)
    else:
        LOG.error('could not destroy workspace %s - %s - %s',
                  workspace_id, response.status_code, response.text)


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
    LOG.debug('deleting %s', test_pool)
    random.shuffle(test_pool)
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONFIG['thread_pool_size']) as executor:
        for test_path in test_pool:
            executor.submit(delete_workspace, test_path)


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
    if 'workspace_delete_timeout' in config:
        WORKSPACE_DELETE_TIMEOUT = config['workspace_delete_timeout']
    if 'workspace_status_poll_interval' in config:
        WORKSPACE_STATUS_POLL_INTERVAL = config['workspace_status_poll_interval']
    if 'workspace_retry_interval' in config:
        WORKSPACE_RETRY_INTERVAL = config['workspace_retry_interval']


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
