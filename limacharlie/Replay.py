from gevent import monkey; monkey.patch_all()
from gevent.lock import BoundedSemaphore
from . import Manager
from .utils import LcApiException

import os
import os.path
import uuid
import urllib2
import base64
import json
import yaml
import gevent
import time
import sys
import time

class Replay( object ):
    '''Interface to query historical sensor data in Insight with specific D&R rules.'''

    def __init__( self, manager, maxTimeWindow = ( 60 * 60 * 24 * 1 ), maxConcurrent = 10, isInteractive = False ):
        self._lc = manager
        self._apiURL = None
        self._maxTimeWindow = maxTimeWindow
        self._maxConcurrent = maxConcurrent
        self._replayURL = self._lc.getOrgURLs()[ 'replay' ]

        self._isInteracive = isInteractive
        self._statusMutex = BoundedSemaphore()
        self._queryPending = 0
        self._sensorPending = 0
        if self._isInteracive:
            self._queryStartedAt = time.time()
            gevent.spawn_later( 0, self._reportStatus )

    def _reportStatus( self ):
        with self._statusMutex:
            sys.stdout.write( "\rSensors pending: %8s, queries pending: %8s, elapsed: %8.2f seconds" % ( self._sensorPending, self._queryPending, time.time() - self._queryStartedAt ) )
        sys.stdout.flush()

        gevent.spawn_later( 1, self._reportStatus )

    def scanHistoricalSensor( self, sid, startTime, endTime, ruleName = None, ruleContent = None ):
        '''Scan a specific sensor's data with a D&R rule.

        Args:
            sid (str): sensor ID to scan.
            startTime (int): seconds epoch to start scanning at.
            endTime (int): seconds epoch to stop scanning at.
            ruleName (str): the name of an existing D&R rule to use.
            ruleContent (dict): D&R rule to use to scan, with a "detect" key and a "respond" key.

        Returns:
            a dict containing results of the query.
        '''
        windows = []

        with self._statusMutex:
            if 0 == self._sensorPending:
                self._sensorPending = 1

        try:
            # Split up the total time into windows we can query in parallel.
            if endTime - startTime > self._maxTimeWindow:
                tmpStart = startTime
                while tmpStart < endTime:
                    tmpEnd = min( tmpStart + self._maxTimeWindow, endTime )
                    windows.append( ( tmpStart, tmpEnd ) )
                    tmpStart += self._maxTimeWindow
            else:
                windows = [ ( startTime, endTime ) ]

            with self._statusMutex:
                self._queryPending += len( windows )

            results = self._parallelExec( lambda w: self._scanHistoricalSensor( sid, w[ 0 ], w[ 1 ], ruleName = ruleName, ruleContent = ruleContent ), windows, maxConcurrent = self._maxConcurrent )
        finally:
            with self._statusMutex:
                self._sensorPending -= 1

        return self._rollupResults( results )

    def _scanHistoricalSensor( self, sid, startTime, endTime, ruleName = None, ruleContent = None ):
        # print( "Starting query %s-%s for %s" % ( startTime, endTime, sid ) )
        qStart = time.time()

        try:
            if self._apiURL is None:
                # Get the ingest URL from the API.
                self._apiURL = 'https://%s/' % ( self._replayURL, )
            req = {
                'start' : startTime,
                'end' : endTime,
            }
            body = None
            if ruleName is not None:
                req[ 'rule_name' ] = ruleName
            elif ruleContent is not None:
                body = json.dumps( ruleContent )
            else:
                raise LcApiException( 'no rule specified' )

            statusCode, resp = self._lc._restCall( 'sensor/%s/%s' % ( self._lc._oid, sid, ),
                                                   'POST',
                                                   {},
                                                   altRoot = self._apiURL,
                                                   queryParams = req,
                                                   rawBody = body,
                                                   contentType = 'application/json' )

            if 200 != statusCode:
                raise LcApiException( '%s: %s' % ( statusCode, resp ) )
        finally:
            with self._statusMutex:
                self._queryPending -= 1
        # print( "Finished query %s-%s for %s in %s seconds" % ( startTime, endTime, sid, time.time() - qStart ) )

        return resp

    def scanEntireOrg( self, startTime, endTime, ruleName = None, ruleContent = None ):
        '''Scan an entire organization's data with a D&R rule.

        Args:
            startTime (int): seconds epoch to start scanning at.
            endTime (int): seconds epoch to stop scanning at.
            ruleName (str): the name of an existing D&R rule to use.
            ruleContent (dict): D&R rule to use to scan, with a "detect" key and a "respond" key.

        Returns:
            a dict containing results of the query.
        '''
        sensors = self._lc.sensors()
        while True:
            moreSensors = self._lc.sensors( is_next = True )
            if moreSensors is None:
                break
            sensors += moreSensors

        with self._statusMutex:
            self._sensorPending = len( sensors )

        results = self._parallelExec( lambda sid: self.scanHistoricalSensor( sid, startTime, endTime, ruleName = ruleName, ruleContent = ruleContent ), sensors, maxConcurrent = self._maxConcurrent )

        return self._rollupResults( results )

    def _rollupResults( self, results ):
        final = {}
        for result in results:
            if not isinstance( result, dict ):
                raise result
            for k, v in result.iteritems():
                if isinstance( v, ( int, float ) ):
                    if k not in final:
                        final[ k ] = 0
                    final[ k ] += v
                elif isinstance( v, ( str, unicode, bytes ) ):
                    final.setdefault( k, [] ).append( v )
                elif isinstance( v, dict ):
                    final.setdefault( k, {} ).update( v )
                elif isinstance( v, ( list, tuple ) ):
                    tmp = final.setdefault( k, [] )
                    tmp += list( v )
                else:
                    raise LcApiException( 'unexpected data type: %s' % ( type( v ), ) )
        return final

    def _parallelExec( self, f, objects, timeout = None, maxConcurrent = None ):
        g = gevent.pool.Pool( size = maxConcurrent )
        results = g.imap_unordered( lambda o: self._retExecOrExc( f, o, timeout ), objects )
        return list( results )

    def _retExecOrExc( self, f, o, timeout ):
        try:
            if timeout is None:
                return f( o )
            else:
                with gevent.Timeout( timeout ):
                    return f( o )
        except ( Exception, gevent.Timeout ) as e:
            return e

def main():
    import argparse
    import getpass

    parser = argparse.ArgumentParser( prog = 'limacharlie.io replay detection and response' )

    parser.add_argument( '--sid',
                         type = uuid.UUID,
                         required = False,
                         dest = 'sid',
                         default = None,
                         help = 'sensor id to scan traffic from.' )

    parser.add_argument( '--entire-org',
                         action = 'store_true',
                         default = False,
                         required = False,
                         dest = 'isEntireOrg',
                         help = 'if set and --sid is not set, replay traffic from entire organization.' )

    parser.add_argument( '--start',
                         type = int,
                         required = False,
                         dest = 'start',
                         default = None,
                         help = 'epoch seconds at which to start scanning sensor traffic.' )

    parser.add_argument( '--end',
                         type = int,
                         required = False,
                         dest = 'end',
                         default = None,
                         help = 'epoch seconds at which to end scanning sensor traffic.' )

    parser.add_argument( '--rule-name',
                         type = str,
                         required = False,
                         dest = 'ruleName',
                         default = None,
                         help = 'name of the an already-existing rule to scan with.' )

    parser.add_argument( '--rule-content',
                         type = str,
                         required = False,
                         dest = 'ruleContent',
                         default = None,
                         help = 'file path where rule to scan is.' )

    parser.add_argument( '--max-time-window',
                         type = int,
                         required = False,
                         dest = 'maxTimeWindow',
                         default = ( 60 * 60 * 24 * 1 ),
                         help = 'maximum number of seconds in a window used to shard the search.' )

    parser.add_argument( '--max-concurrent',
                         type = int,
                         required = False,
                         dest = 'maxConcurrent',
                         default = 10,
                         help = 'maximum number of concurrent queries per sensor searched.' )

    parser.add_argument( '--last-seconds',
                         type = int,
                         required = False,
                         dest = 'lastSeconds',
                         default = None,
                         help = 'can be specified instead of --start and --end, will make the time window the last X seconds.' )

    args = parser.parse_args()

    replay = Replay( Manager( None, None ),
                     isInteractive = True,
                     maxTimeWindow = args.maxTimeWindow,
                     maxConcurrent = args.maxConcurrent )

    ruleContent = None
    if args.ruleContent is not None:
        with open( args.ruleContent, 'rb' ) as f:
            ruleContent = f.read()
        try:
            ruleContent = yaml.safe_load( ruleContent )
        except:
            try:
                ruleContent = json.loads( ruleContent )
            except:
                raise LcApiException( 'rule content not valid yaml or json' )

    if ( args.start is None or args.end is None ) and args.lastSeconds is None:
        raise LcApiException( 'must specify start and end, or last-seconds' )

    start = args.start
    end = args.end
    if start is None and end is None and args.lastSeconds is not None:
        now = int( time.time() )
        start = now - args.lastSeconds
        end = now

    if args.sid is not None:
        response = replay.scanHistoricalSensor( str( args.sid ),
                                                start,
                                                end,
                                                ruleName = args.ruleName,
                                                ruleContent = ruleContent )
    elif args.isEntireOrg:
        response = replay.scanEntireOrg( start,
                                         end,
                                         ruleName = args.ruleName,
                                         ruleContent = ruleContent )
    else:
        raise LcApiException( '--sid or --entire-org must be specified' )

    print( json.dumps( response, indent = 2 ) )