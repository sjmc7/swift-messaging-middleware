Middleware for oslo.messaging notifications in swift
====================================================

This middleware emits oslo.messaging notifications following successful
modification operations (metadata changes to accounts, containers or objects,
and creation or deletion of containers and objects). It requires
oslo.messaging to be installed along with any dependencies, including a
suitable driver (rabbitmq, qpid, etc). In its current incarnation it also
requires middleware that will identify a tenant to which the request refers
(typically keystone's middleware).

The notification payload is dependent on the type of event, but always
includes project_id, container, obj. For events other than deletion,
timestamps, content lengths, metadata may be included.

Installation
------------
Install this package into the virtualenv running swift's proxy server.

oslo.messaging also needs to be installed. This has been tested with
version 2.8.1.

Configuration
-------------
To configure, in proxy-server.conf:

    [filter:notificationmiddleware]
    paste.filter_factory = swift_messaging_middleware.middleware:filter_factory

    publisher_id = swift.localhost
    transport_url = rabbit://user:password@ip:port/
    notification_driver = messaging
    notification_topics = notifications

Additionally, in pipeline:main, add notificationmiddleware to the pipeline;
it should be added towards the end to ensure any required environment
information is present when it runs.

publisher_id will set the exchange name. transport_url instructs
oslo_messaging how to connect to a broker. Since swift doesn't use oslo.config
the full range of oslo.messaging options aren't available, which should be
addressed.

