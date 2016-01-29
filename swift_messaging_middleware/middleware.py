# Copyright (c) 2015 Hewlett-Packard Enterprise Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from datetime import datetime
import logging
from oslo_config import cfg
import oslo_messaging
import six
from swift.common.swob import HTTPForbidden, HTTPBadRequest, \
    HTTPRequestEntityTooLarge, Request
from swift.common.wsgi import make_env, make_pre_authed_env, make_pre_authed_request
from swift.common import wsgi


CONF = cfg.CONF


class OsloMessagingContext(wsgi.WSGIContext):
    def __init__(self, app, notifier):
        wsgi.WSGIContext.__init__(self, app)
        self._notifier = notifier
        self.agent = "%(orig)s OsloMessaging"

    def _timestamp_to_str(self, timestamp):
        dt = datetime.fromtimestamp(float(timestamp))
        return dt.strftime('%Y-%m-%dT%H:%M:%S.%f')

    def _get_metadata(self, request_headers, prefix, include_remove=True):
        added_prefix = 'X-%s-Meta-' % prefix
        removed_prefix = 'X-Remove-%s-Meta-' % prefix
 
        return {k.lower(): v 
                for k, v in six.iteritems(request_headers) 
                if k.startswith(added_prefix) or 
                   (include_remove and k.startswith(removed_prefix))}

    def _get_object_metadata(self, request_headers, response_headers):
        object_metadata = self._get_metadata(request_headers, 'Object')
        mtime = object_metadata.get('x-object-meta-mtime')
        if mtime:
            object_metadata['X-Object-Meta-Mtime'] = self._timestamp_to_str(mtime)
        etag_headers = filter(lambda h: h[0] == 'Etag', response_headers)
        if etag_headers:
            object_metadata['hash'] = etag_headers[0][1]
        return object_metadata

    def _get_container_metadata(self, request_headers, response_headers):
        return self._get_metadata(request_headers, 'Container')

    def _get_account_metadata(self, request_headers, response_headers):
        return self._get_metadata(request_headers, 'Account')

    def _get_request_auth_info(self, request_headers):
        return {
                'project_id': request_headers.get('X-Project-Id'),
                'project_name': request_headers.get('X-Project-Name'),
                'project_domain_id': request_headers.get('X-Project-Domain-Id'),
                'project_domain_name': request_headers.get('X-Project-Domain-Name'),
                'x-trans-id': request_headers.get('X-Trans-Id')
        }

    def _make_head_request(self, env):
        tmp_req = make_pre_authed_request(env, method="HEAD")
        resp = tmp_req.get_response(self.app)
        return resp

    def handle_request(self, env, start_response):
        request = Request(env)
        method = request.method
        if method not in ('POST', 'PUT', 'COPY', 'DELETE'):
            return self.app(env, start_response)

        # Get the response from the rest of the pipeline before we
        # start doing anything; this means that whatever is being created
        # or deleted will have been done before we start constructing
        # the notification payload
        response = self._app_call(env)
        status_code = self._get_status_int()

        try:
            ver, account, container, obj = request.split_path(
                2, 4, rest_with_last=True)
        except ValueError:
            start_response(self._response_status,
                       self._response_headers,
                       self._response_exc_info)
            return response

        event_methods = {
            'DELETE': 'delete',
            'COPY': 'copy',
            'PUT': 'create',
            'POST': 'metadata'
        }
        event_object = ('object' if obj
                        else 'container' if container
                        else 'account')

        event_type = '%s.%s' % (event_object, event_methods[method])

        if status_code in (200, 201, 202, 204):
            request_headers = request.headers

            payload = self._get_request_auth_info(request_headers)
            payload['account'] = account
            if container:
                payload['container'] = container
                if obj:
                    payload['object'] = obj


            if method != 'DELETE':
                head_headers = self._make_head_request(env).headers

                copy_from = request_headers.get('X-Copy-From')
                if copy_from:
                    # Copies are turned into PUTs with an X-Copy-From in the object middleware
                    # though we don't need to handle them differently
                    event_type = event_methods['COPY']
                    if copy_from[0] == '/':
                        copy_from = copy_from[1:]
                    copy_from_container, copy_from_object = copy_from.split('/', 1)

                    payload['copy_from_container'] = copy_from_container
                    payload['copy_from_object'] = copy_from_object

                    if request_headers.get('X-Fresh-Metadata', None):
                        payload['copy_fresh_metadata'] = bool(request_headers.get('X-Fresh-Metadata'))

                payload.update(self._get_account_metadata(request_headers, self._response_headers))
                if container:
                    payload.update(self._get_container_metadata(request_headers, self._response_headers))

                    if obj:
                        payload.update(self._get_object_metadata(request_headers, self._response_headers))

                modified_timestamp = head_headers.get('X-Timestamp')
                if modified_timestamp:
                    modified_datetime = datetime.fromtimestamp(float(modified_timestamp))
                    payload['updated_at'] = modified_datetime.strftime('%Y-%m-%dT%H:%M:%S.%f')

                last_modified = head_headers.get('Last-Modified')
                if last_modified:
                    payload['last_modified'] = last_modified
                
                content_length = head_headers.get('Content-Length')
                if obj and content_length:
                    payload['content_length'] = content_length

            self._notifier.info({}, event_type, payload)


        # We don't want to tamper with the response
        start_response(self._response_status,
                       self._response_headers,
                       self._response_exc_info)
        return response


class OsloMessagingMiddleware(object):
    def __init__(self, app, conf):
        self._app = app
        self._transport = oslo_messaging.get_transport(
            CONF,
            url=conf['transport_url'])
        self._notifier = oslo_messaging.Notifier(
            self._transport,
            driver=conf['notification_driver'],
            publisher_id=conf['publisher_id'],
            topic=conf['notification_topics'])

    def __call__(self, env, start_response):
        messaging_context = OsloMessagingContext(self._app, self._notifier)
        return messaging_context.handle_request(env, start_response)


def filter_factory(global_conf, **local_conf):
    conf = global_conf.copy()
    conf.update(local_conf)
    def oslo_messaging_filter(app):
        return OsloMessagingMiddleware(app, conf)
    return oslo_messaging_filter
