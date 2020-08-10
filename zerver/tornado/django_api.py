import logging
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union

import requests
import ujson
from django.conf import settings

from zerver.lib.queue import queue_json_publish
from zerver.models import Client, Realm, UserProfile
from zerver.tornado.event_queue import process_notification
from zerver.tornado.sharding import get_tornado_port, get_tornado_uri, notify_tornado_queue_name

requests_client = requests.Session()
for host in ['127.0.0.1', 'localhost']:
    if settings.TORNADO_SERVER and host in settings.TORNADO_SERVER:
        # This seems like the only working solution to ignore proxy in
        # requests library.
        requests_client.trust_env = False

def request_event_queue(user_profile: UserProfile, user_client: Client, apply_markdown: bool,
                        client_gravatar: bool, slim_presence: bool, queue_lifespan_secs: int,
                        event_types: Optional[Iterable[str]]=None,
                        all_public_streams: bool=False,
                        narrow: Iterable[Sequence[str]]=[],
                        bulk_message_deletion: bool=False) -> Optional[str]:

    if settings.TORNADO_SERVER:
        tornado_uri = get_tornado_uri(user_profile.realm)
        req = {'dont_block': 'true',
               'apply_markdown': ujson.dumps(apply_markdown),
               'client_gravatar': ujson.dumps(client_gravatar),
               'slim_presence': ujson.dumps(slim_presence),
               'all_public_streams': ujson.dumps(all_public_streams),
               'client': 'internal',
               'user_profile_id': user_profile.id,
               'user_client': user_client.name,
               'narrow': ujson.dumps(narrow),
               'secret': settings.SHARED_SECRET,
               'lifespan_secs': queue_lifespan_secs,
               'bulk_message_deletion': ujson.dumps(bulk_message_deletion)}

        if event_types is not None:
            req['event_types'] = ujson.dumps(event_types)

        try:
            resp = requests_client.post(tornado_uri + '/api/v1/events/internal',
                                        data=req)
        except requests.adapters.ConnectionError:
            logging.error('Tornado server does not seem to be running, check %s '
                          'and %s for more information.',
                          settings.ERROR_FILE_LOG_PATH, "tornado.log")
            raise requests.adapters.ConnectionError(
                f"Django cannot connect to Tornado server ({tornado_uri}); try restarting")

        resp.raise_for_status()

        return resp.json()['queue_id']

    return None

def get_user_events(user_profile: UserProfile, queue_id: str, last_event_id: int) -> List[Dict[str, Any]]:
    if settings.TORNADO_SERVER:
        tornado_uri = get_tornado_uri(user_profile.realm)
        post_data: Dict[str, Any] = {
            'queue_id': queue_id,
            'last_event_id': last_event_id,
            'dont_block': 'true',
            'user_profile_id': user_profile.id,
            'secret': settings.SHARED_SECRET,
            'client': 'internal',
        }
        resp = requests_client.post(tornado_uri + '/api/v1/events/internal',
                                    data=post_data)
        resp.raise_for_status()

        return resp.json()['events']
    return []

def send_notification_http(realm: Realm, data: Mapping[str, Any]) -> None:
    if settings.TORNADO_SERVER and not settings.RUNNING_INSIDE_TORNADO:
        tornado_uri = get_tornado_uri(realm)
        requests_client.post(tornado_uri + '/notify_tornado', data=dict(
            data   = ujson.dumps(data),
            secret = settings.SHARED_SECRET))
    else:
        process_notification(data)

def send_event(realm: Realm, event: Mapping[str, Any],
               users: Union[Iterable[int], Iterable[Mapping[str, Any]]]) -> None:
    """`users` is a list of user IDs, or in the case of `message` type
    events, a list of dicts describing the users and metadata about
    the user/message pair."""
    port = get_tornado_port(realm)
    queue_json_publish(notify_tornado_queue_name(port),
                       dict(event=event, users=list(users)),
                       lambda *args, **kwargs: send_notification_http(realm, *args, **kwargs))
