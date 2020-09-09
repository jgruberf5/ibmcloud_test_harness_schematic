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
import shutil
import math
import glob
import json
import logging
import datetime
import time
import tarfile
import uuid
import copy

LOG = logging.getLogger('ibmcloud_test_harness_build')
LOG.setLevel(logging.DEBUG)
FORMATTER = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
LOGSTREAM = logging.StreamHandler(sys.stdout)
LOGSTREAM.setFormatter(FORMATTER)
LOG.addHandler(LOGSTREAM)

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

TEMPLATE_DIR = "%s/templates" % SCRIPT_DIR
QUEUE_DIR = "%s/queued_tests" % SCRIPT_DIR

CONFIG_FILE = "%s/builder-config.json" % SCRIPT_DIR
CONFIG = {}


def licenses_available():
    if os.path.exists(CONFIG['license_file']):
        return sum(1 for line in open(CONFIG['license_file']))


def get_license():
    if os.path.exists(CONFIG['license_file']) and \
       os.stat(CONFIG['license_file']).st_size > 0:
        with open(CONFIG['license_file'], 'r+') as f:
            firstLine = f.readline()
            while len(firstLine) < 5:
                firstLine = f.readline()
            data = f.read()
            f.seek(0)
            f.write(data)
            f.truncate()
        return firstLine.strip()
    else:
        return ''


def region_from_zone(zone):
    parts = zone.split('-')
    return "%s-%s" % (parts[0], parts[1])


def get_template_types():
    os.chdir(TEMPLATE_DIR)
    templates = []
    for file in glob.glob("*.gz"):
        templates.append(os.path.basename(file).split('.')[0])
    os.chdir(SCRIPT_DIR)
    return templates


def build_utility():
    zone_resources = {}
    with open(CONFIG['zone_resources_file'], 'r') as zrf:
        zone_resources = json.load(zrf)
    schematics_regions = {}
    with open(CONFIG['schematics_regions_file'], 'r') as srf:
        schematics_regions = json.load(srf)
    zones = os.listdir(QUEUE_DIR)
    test_map = {}
    for zone in zones:
        if zone in CONFIG['active_zones']:
            if not zone in test_map:
                test_map[zone] = {'test_count': 0}
    number_of_active_zones = len(test_map)
    number_per_zone = CONFIG['utility_pool_tests_per_zone']
    total_tests = 0
    total_zones = 0
    indexed_images = {}
    test_per_zone = {}
    for zone in zones:
        if zone in CONFIG['active_zones']:
            LOG.info('creating tests in zone: %s' % zone)
            test_per_zone[zone] = {}
            test_map[zone]['test_to_create'] = number_per_zone
            zone_dir = os.path.join(QUEUE_DIR, zone)
            images = os.listdir(zone_dir)
            total_zones = total_zones + 1
            adding_test = True
            while adding_test:
                adding_test = False
                for image in images:
                    if image.endswith(zone):
                        base_image_name = image[0:-(len(zone)+1)]
                    else:
                        base_image_name = image
                    image_eligible = False
                    for match in CONFIG['active_images']:
                        if image.find(match) > 0:
                            image_eligible = True
                    if image_eligible:
                        if base_image_name not in indexed_images:
                            indexed_images[image[0:-(len(zone)+1)]] = True
                        if base_image_name not in test_per_zone[zone]:
                            test_per_zone[zone][base_image_name] = 0
                        if test_per_zone[zone][base_image_name] < number_per_zone:
                            test_per_zone[zone][base_image_name] = test_per_zone[zone][base_image_name] + 1
                            adding_test = True
                            LOG.debug('setting up test for image: %s in %s (%d)',
                                      image, zone, test_per_zone[zone][base_image_name])
                            image_dir = os.path.join(zone_dir, image)
                            size = ''
                            for sstr in CONFIG['profile_selection']:
                                if image.find(sstr) > 0:
                                    size = CONFIG['profile_selection'][sstr]
                            temp_dir = os.path.join(image_dir, 'schematic')
                            test_id = str(uuid.uuid4())
                            test_dir = os.path.join(temp_dir, test_id)
                            # LOG.info('creating test: %s' % test_dir)
                            os.mkdir(test_dir)
                            ssh_key_name = zone_resources[zone]['ssh_key_name']['value']
                            if 'global_ssh_key' in CONFIG and CONFIG['global_ssh_key']:
                                ssh_key_name = CONFIG['global_ssh_key']
                            variables = {
                                "instance_name": "t-%s" % test_id,
                                "tmos_image_name": image,
                                "instance_profile": size,
                                "ssh_key_name": ssh_key_name,
                                "license_type": "utilitypool",
                                "byol_license_basekey": "",
                                "license_host": CONFIG['zone_license_hosts'][zone]['license_host'],
                                "license_username": CONFIG['zone_license_hosts'][zone]['license_username'],
                                "license_password": CONFIG['zone_license_hosts'][zone]['license_password'],
                                "license_pool": CONFIG['zone_license_hosts'][zone]['license_pool'],
                                "license_sku_keyword_1": CONFIG['zone_license_hosts'][zone]['license_sku_keyword_1'],
                                "license_sku_keyword_2": CONFIG['zone_license_hosts'][zone]['license_sku_keyword_2'],
                                "license_unit_of_measure": CONFIG['zone_license_hosts'][zone]['license_unit_of_measure'],
                                "tmos_admin_password": "f5c0nfig",
                                "management_subnet_id": zone_resources[zone]['f5_management_id']['value'],
                                "data_1_1_subnet_id": zone_resources[zone]['f5_cluster_id']['value'],
                                "data_1_2_subnet_id": zone_resources[zone]['f5_internal_id']['value'],
                                "data_1_3_subnet_id": zone_resources[zone]['f5_external_id']['value'],
                                "data_1_4_subnet_id": "",
                                "phone_home_url": "%s/stop/%s" % (CONFIG['report_service_base_url'], test_id),
                                "template_version": "09082020",
                                "app_id": "%s/report/%s" % (CONFIG['report_service_base_url'], test_id)
                            }
                            create_template = copy.deepcopy(schematics_regions[region_from_zone(zone)]['create_template'])
                            create_template['name'] = "w-%s" % test_id
                            variablestore = list(create_template['template_data'][0]['variablestore'])
                            for k in variables.keys():
                                variablestore.append({
                                    "name": k,
                                    "value": variables[k]
                                })
                            create_template['template_data'][0]['variablestore'] = variablestore
                            with open(os.path.join(test_dir, 'create_data.json'), 'w') as cdata:
                                cdata.write(json.dumps(create_template, indent=4, separators=(',', ': ')))
                            regional_endpoint = schematics_regions[region_from_zone(
                                zone)]['endpoint_url']
                            with open(os.path.join(test_dir, 'service_endpoint.url'), 'w') as se:
                                se.write(regional_endpoint)
                                total_tests = total_tests + 1
    LOG.info("%d total tests created in %d zones for %d images",
             total_tests, total_zones, len(indexed_images.keys()))


def build_byol():
    zone_resources = {}
    with open(CONFIG['zone_resources_file'], 'r') as zrf:
        zone_resources = json.load(zrf)
    schematics_regions = {}
    with open(CONFIG['schematics_regions_file'], 'r') as srf:
        schematics_regions = json.load(srf)
    zones = os.listdir(QUEUE_DIR)
    num_licenses = licenses_available()
    test_map = {}
    for zone in zones:
        if zone in CONFIG['active_zones']:
            if not zone in test_map:
                test_map[zone] = {'test_count': 0}
    number_of_active_zones = len(test_map)
    number_per_zone = num_licenses / number_of_active_zones
    number_license_left = num_licenses % number_of_active_zones
    for zone in zones:
        if zone in CONFIG['active_zones']:
            test_map[zone]['test_to_create'] = number_per_zone
            zone_dir = os.path.join(QUEUE_DIR, zone)
            images = os.listdir(zone_dir)
            while test_map[zone]['test_to_create'] > 0:
                for image in images:
                    image_eligible = False
                    for match in CONFIG['active_images']:
                        if image.find(match) > 0:
                            image_eligible = True
                    if image_eligible:
                        image_dir = os.path.join(zone_dir, image)
                        size = ''
                        for sstr in CONFIG['profile_selection']:
                            if image.find(sstr) > 0:
                                size = CONFIG['profile_selection'][sstr]
                        temp_dir = os.path.join(image_dir, 'schematics')
                        test_id = str(uuid.uuid4())
                        test_dir = os.path.join(temp_dir, test_id)
                        license = get_license()
                        # LOG.info('creating test: %s' % test_dir)
                        os.mkdir(test_dir)
                        ssh_key_name = zone_resources[zone]['ssh_key_name']['value']
                        if 'global_ssh_key' in CONFIG and CONFIG['global_ssh_key']:
                            ssh_key_name = CONFIG['global_ssh_key']
                        variables = {
                            "instance_name": "t-%s" % test_id,
                            "tmos_image_name": image,
                            "instance_profile": size,
                            "ssh_key_name": ssh_key_name,
                            "license_type": "byol",
                            "byol_license_basekey": license,
                            "license_host": "",
                            "license_username": "",
                            "license_password": "",
                            "license_pool": "",
                            "license_sku_keyword_1": "",
                            "license_sku_keyword_2": "",
                            "license_unit_of_measure": "",
                            "tmos_admin_password": "f5c0nfig",
                            "management_subnet_id": zone_resources[zone]['f5_management_id']['value'],
                            "data_1_1_subnet_id": zone_resources[zone]['f5_cluster_id']['value'],
                            "data_1_2_subnet_id": zone_resources[zone]['f5_internal_id']['value'],
                            "data_1_3_subnet_id": zone_resources[zone]['f5_external_id']['value'],
                            "data_1_4_subnet_id": "",
                            "phone_home_url": "%s/stop/%s" % (CONFIG['report_service_base_url'], test_id),
                            "template_version": "09082020",
                            "app_id": "%s/report/%s" % (CONFIG['report_service_base_url'], test_id)
                        }
                        create_template = copy.deepcopy(schematics_regions[region_from_zone(zone)]['create_template'])
                        create_template['name'] = "w-%s" % test_id
                        variablestore = list(create_template['template_data'][0]['variablestore'])
                        for k in variables.keys():
                            variablestore.append({
                                "name": k,
                                "value": variables[k]
                            })
                        create_template['template_data'][0]['variablestore'] = variablestore
                        with open(os.path.join(test_dir, 'create_data.json'), 'w') as cdata:
                            cdata.write(json.dumps(create_template, indent=4, separators=(',', ': ')))
                        regional_endpoint = schematics_regions[region_from_zone(
                            zone)]['endpoint_url']
                        with open(os.path.join(test_dir, 'service_endpoint.url'), 'w') as se:
                            se.write(regional_endpoint)
                        test_map[zone]['test_to_create'] = test_map[zone]['test_to_create'] - 1


def build_tests():
    if CONFIG['license_type'] == 'byol':
        num_licenses = licenses_available()
        LOG.info('%d BYOL licenses available for test queuing', num_licenses)
        while num_licenses > 0:
            build_byol()
            num_licenses = licenses_available()
    if CONFIG['license_type'] == 'utilitypool':
        build_utility()


def initialize():
    global CONFIG
    os.makedirs(QUEUE_DIR, exist_ok=True)
    config_json = ''
    with open(CONFIG_FILE, 'r') as cf:
        config_json = cf.read()
    config = json.loads(config_json)
    if not config['license_file'].startswith('/'):
        config['license_file'] = "%s/%s" % (SCRIPT_DIR, config['license_file'])
    if not config['f5_images_catalog_file'].startswith('/'):
        config['f5_images_catalog_file'] = "%s/%s" % (
            SCRIPT_DIR, config['f5_images_catalog_file'])
    if not config['zone_resources_file'].startswith('/'):
        config['zone_resources_file'] = "%s/%s" % (
            SCRIPT_DIR, config['zone_resources_file'])
    if not config['schematics_regions_file'].startswith('/'):
        config['schematics_regions_file'] = "%s/%s" % (
            SCRIPT_DIR, config['schematics_regions_file'])
    images_json = ''
    with open(config['f5_images_catalog_file'], 'r') as imf:
        images_json = imf.read()
    images = json.loads(images_json)
    for zone in config['active_zones']:
        zone_queue = "%s/%s" % (QUEUE_DIR, zone)
        os.makedirs(zone_queue, exist_ok=True)
        region = region_from_zone(zone)
        if region in images:
            for image in images[region]:
                image_queue = "%s/%s" % (zone_queue, image['image_name'])
                os.makedirs(image_queue, exist_ok=True)
                template_queue = "%s/%s" % (image_queue, 'schematic')
                os.makedirs(template_queue, exist_ok=True)
    CONFIG = config


if __name__ == "__main__":
    START_TIME = time.time()
    LOG.debug('process start time: %s', datetime.datetime.fromtimestamp(
        START_TIME).strftime("%A, %B %d, %Y %I:%M:%S"))
    initialize()
    ERROR_MESSAGE = ''
    ERROR = False
    build_tests()
    STOP_TIME = time.time()
    DURATION = STOP_TIME - START_TIME
    LOG.debug(
        'process end time: %s - ran %s (seconds)',
        datetime.datetime.fromtimestamp(
            STOP_TIME).strftime("%A, %B %d, %Y %I:%M:%S"),
        DURATION
    )
