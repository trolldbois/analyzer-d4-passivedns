#!/usr/bin/env python3
#
# pdns-ingestion is the D4 analyzer for the Passive DNS backend.
#
# This software parses input (via a Redis list) from a D4 server and
# ingest it into a redis compliant server to server the records for
# the passive DNS at later stage.
#
# This software is part of the D4 project.
#
# The software is released under the GNU Affero General Public version 3.
#
# Copyright (c) 2019 Alexandre Dulaunoy - a@foo.be
# Copyright (c) Computer Incident Response Center Luxembourg (CIRCL)


import re
import redis
import fileinput
import json
import configparser
import time
import logging
import sys
import os

config = configparser.RawConfigParser()
config.read('../etc/analyzer.conf')

expirations = config.items('expiration')
excludesubstrings = config.get('exclude', 'substring').split(',')
myuuid = config.get('global', 'my-uuid')
myqueue = "analyzer:8:{}".format(myuuid)
mylogginglevel = config.get('global', 'logging-level')
logger = logging.getLogger('pdns ingestor')
ch = logging.StreamHandler()
if mylogginglevel == 'DEBUG':
    logger.setLevel(logging.DEBUG)
    ch.setLevel(logging.DEBUG)
elif mylogginglevel == 'INFO':
    logger.setLevel(logging.INFO)
    ch.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)

logger.info("Starting and using FIFO {} from D4 server".format(myqueue))

analyzer_redis_host = os.getenv('D4_ANALYZER_REDIS_HOST', '127.0.0.1')
analyzer_redis_port = int(os.getenv('D4_ANALYZER_REDIS_PORT', 6400))

d4_server, d4_port = config.get('global', 'd4-server').split(':')
host_redis_metadata = os.getenv('D4_REDIS_METADATA_HOST', d4_server)
port_redis_metadata = int(os.getenv('D4_REDIS_METADATA_PORT', d4_port))

r = redis.Redis(host=analyzer_redis_host, port=analyzer_redis_port)
r_d4 = redis.Redis(host=host_redis_metadata, port=port_redis_metadata, db=2)


with open('../etc/records-type.json') as rtypefile:
    rtype = json.load(rtypefile)

dnstype = {}

stats = True

for v in rtype:
    dnstype[(v['type'])] = v['value']


def process_format_passivedns(line=None):
    # log line example
    # timestamp||ip-src||ip-dst||class||q||type||v||ttl||count
    # 1548624738.280922||192.168.1.12||8.8.8.8||IN||www-google-analytics.l.google.com.||AAAA||2a00:1450:400e:801::200e||299||12
    vkey = ['timestamp','ip-src','ip-dst','class','q','type','v','ttl','count']
    record = {}
    if line is None or line == '':
        return False
    v = line.split("||")
    i = 0
    for r in v:
        # trailing dot is removed and avoid case sensitivity
        if i == 4 or i == 6:
            r = r[:-1]
            r = r.lower()
        # timestamp is just epoch - second precision is only required
        if i == 0:
            r = r.split('.')[0]
        record[vkey[i]] = r
        # replace DNS type with the known DNS record type value
        if i == 5:
            record[vkey[i]] = dnstype[r]
        i = i + 1
    return record


while (True):
    expiration = None
    d4_record_line = r_d4.rpop(myqueue)
    if d4_record_line is None:
        time.sleep (1)
        continue
    l = d4_record_line.decode('utf-8')
    rdns = process_format_passivedns(line=l.strip())
    logger.debug("parsed record: {}".format(rdns))
    if rdns is False:
        logger.debug('Parsing of passive DNS line failed: {}'.format(l.strip()))
        continue
    if 'q' not in rdns:
        logger.debug('Parsing of passive DNS line is incomplete: {}'.format(l.strip()))
        continue
    if rdns['q'] and rdns['type']:
        excludeflag = False
        for exclude in excludesubstrings:
            if exclude in rdns['q']:
               excludeflag = True
        if excludeflag:
            logger.debug('Excluded {}'.format(rdns['q']))
            continue
        for y in expirations:
            if y[0] == rdns['type']:
                expiration=y[1]
        if rdns['type'] == '16':
            rdns['v'] = rdns['v'].replace("\"", "", 1)
        query = "r:{}:{}".format(rdns['q'],rdns['type'])
        logger.debug('redis sadd: {} -> {}'.format(query,rdns['v']))
        r.sadd(query, rdns['v'])
        if expiration:
            logger.debug("Expiration {} {}".format(expiration, query))
            r.expire(query, expiration)
        res = "v:{}:{}".format(rdns['v'], rdns['type'])
        logger.debug('redis sadd: {} -> {}'.format(res,rdns['q']))
        r.sadd(res, rdns['q'])
        if expiration:
            logger.debug("Expiration {} {}".format(expiration, query))
            r.expire(res, expiration)

        firstseen = "s:{}:{}:{}".format(rdns['q'], rdns['v'], rdns['type'])
        if not r.exists(firstseen):
            r.set(firstseen, rdns['timestamp'])
            logger.debug('redis set: {} -> {}'.format(firstseen, rdns['timestamp']))

        if expiration:
            logger.debug("Expiration {} {}".format(expiration, query))
            r.expire(firstseen, expiration)

        lastseen = "l:{}:{}:{}".format(rdns['q'], rdns['v'], rdns['type'])
        last = r.get(lastseen)
        if last is None or int(last) < int(rdns['timestamp']):
            r.set(lastseen, rdns['timestamp'])
            logger.debug('redis set: {} -> {}'.format(lastseen, rdns['timestamp']))
        if expiration:
            logger.debug("Expiration {} {}".format(expiration, query))
            r.expire(query, expiration)

        occ = "o:{}:{}:{}".format(rdns['q'], rdns['v'], rdns['type'])
        r.incr(occ, amount=1)
        if expiration:
            logger.debug("Expiration {} {}".format(expiration, query))
            r.expire(occ, expiration)



        # TTL, Class, DNS Type distribution stats
        if 'ttl' in rdns:
            r.hincrby('dist:ttl', rdns['ttl'], amount=1)
        if 'class' in rdns:
            r.hincrby('dist:class', rdns['class'], amount=1)
        if 'type' in rdns:
            r.hincrby('dist:type', rdns['type'], amount=1)
        if stats:
            r.incrby('stats:processed', amount=1)
    if not r:
        logger.info('empty passive dns record')
        continue
