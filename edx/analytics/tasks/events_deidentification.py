"""Deidentify course event files by removing/stubbing user information."""

import logging
import re
import luigi
import luigi.date_interval
import gzip
import csv
import cjson
import os
import sys
from collections import namedtuple, defaultdict

from edx.analytics.tasks.pathutil import PathSetTask, EventLogSelectionMixin, EventLogSelectionDownstreamMixin
from edx.analytics.tasks.mapreduce import MultiOutputMapReduceJobTask, MapReduceJobTaskMixin
from edx.analytics.tasks.url import ExternalURL, url_path_join
from edx.analytics.tasks.util.deid_util import DeidentifierMixin, DeidentifierParamsMixin, IMPLICIT_EVENT_TYPE_PATTERNS, UserInfoMixin, UserInfoDownstreamMixin
import edx.analytics.tasks.util.opaque_key_util as opaque_key_util
import edx.analytics.tasks.util.csv_util
from edx.analytics.tasks.util import eventlog

log = logging.getLogger(__name__)

ExplicitEventType = namedtuple("ExplicitEventType", ["event_source", "event_type"])


class DeidentifyCourseEventsTask(DeidentifierMixin, UserInfoMixin, MultiOutputMapReduceJobTask):
    """
    Task to deidentify events for a particular course.

    Uses the output produced by EventExportByCourseTask as source for this task.
    """

    course = luigi.Parameter(default=None)
    explicit_event_whitelist = luigi.Parameter(
        config_path={'section': 'deidentification', 'name': 'explicit_event_whitelist'}
    )
    dump_root = luigi.Parameter(default=None)

    def requires(self):
        filename_safe_course_id = opaque_key_util.get_filename_safe_course_id(self.course)
        event_files_url = url_path_join(self.dump_root, filename_safe_course_id, 'events')
        return PathSetTask([event_files_url], ['*'])

    def requires_local(self):
        results = {}
        if os.path.basename(self.explicit_event_whitelist) != self.explicit_event_whitelist:
            results['explicit_events'] = ExternalURL(url=self.explicit_event_whitelist)

        return results

    def init_local(self):
        super(DeidentifyCourseEventsTask, self).init_local()

        self.explicit_events = []
        if self.input_local().get('explicit_events') is not None:
            with self.input_local()['explicit_events'].open('r') as explicit_events_file:
                self.explicit_events = self.parse_explicit_events_file(explicit_events_file)
        else:
            default_filepath = os.path.join(sys.prefix, 'share', 'edx.analytics.tasks', self.explicit_event_whitelist)
            with open(default_filepath, 'r') as explicit_events_file:
                self.explicit_events = self.parse_explicit_events_file(explicit_events_file)

    def parse_explicit_events_file(self, explicit_events_file):
        """Parse explicit_events_file and load in memory."""

        events = set()
        for line in explicit_events_file:
            if not line.strip():
                continue
            if line.startswith('#'):
                continue
            _, event_source, event_type = line.rstrip('\n').split("\t")
            events.add(ExplicitEventType(event_source, event_type))

        return events

    def mapper(self, line):
        event = eventlog.parse_json_event(line)
        date_string = event['time'].split("T")[0]

        filtered_event = self.filter_event(event)

        if filtered_event is None:
            return

        yield date_string.encode('utf-8'), line.rstrip('\r\n')

    def multi_output_reducer(self, _key, values, output_file):
        with gzip.GzipFile(mode='wb', fileobj=output_file) as outfile:
            try:
                for value in values:
                    filtered_event = eventlog.parse_json_event(value)
                    deidentified_event = self.deidentify_event(filtered_event)
                    if deidentified_event is None:
                        return
                    outfile.write(value.strip())
                    outfile.write('\n')
                    # WARNING: This line ensures that Hadoop knows that our process is not sitting in an infinite loop.
                    # Do not remove it.
                    self.incr_counter('Deidentified Event Exports', 'Raw Bytes Written', len(value) + 1)
            finally:
                outfile.close()

    def output_path_for_key(self, key):
        filename_safe_course_id = opaque_key_util.get_filename_safe_course_id(self.course)

        return url_path_join(
            self.output_root,
            filename_safe_course_id,
            'events',
            '{course}-events-{date}.log.gz'.format(
                course=filename_safe_course_id,
                date=key,
            )
        )

    def filter_event(self, event):
        """Filter event using different event filtering criteria."""
        if event is None:
            return None

        event_type = event.get('event_type')

        event_source = event.get('event_source')

        if event_type is None or event_source is None:
            return None

        # TODO: filter out synthetic events as well.

        if event_source == 'server' and event_type.startswith('/'):
            return self.filter_implicit_event(event)
        elif ExplicitEventType(event_source, event_type) in self.explicit_events:
            return event

        return None

    def filter_implicit_event(self, event):
        """Filter implicit event using the whitelist patterns."""

        event_type = event.get('event_type')

        match = opaque_key_util.COURSE_REGEX.match(event_type)
        if match:
            course_id_string = match.group('course_id')
            event_type = event_type.replace(course_id_string, '(course_id)')

        for included_event_type in IMPLICIT_EVENT_TYPE_PATTERNS:
            match = re.match(included_event_type, event_type)
            if match:
                return event

        return None

    def get_user_id_as_int(self, user_id):
        """Convert possible str value of user_id to int or None."""
        if user_id is not None and not isinstance(user_id, int):
            if len(user_id) == 0:
                user_id = None
            else:
                user_id = int(user_id)
        return user_id

    def get_user_info_for_user_id(self, user_id):
        """Return user_info entry for a user_id, or None if not found."""
        try:
            user_info = self.user_by_id[user_id]
            return user_info
        except KeyError:
            log.error("Unable to find user_id %s in the user_by_id map", user_id)
            return None

    def get_user_info_for_username(self, username):
        """Return user_info entry for a username, or None if not found."""
        try:
            user_info = self.user_by_username[username]
            return user_info
        except KeyError:
            log.error("Unable to find username: %s in the user_by_username map", username)
            return None

    def remap_username(self, username, user_info):
        """Return remapped version of username, or None if not remapped."""
        info = self.get_user_info_for_username(username)
        if info is not None:
            for key, value in info.iteritems():
                user_info[key].add(value)
            if 'user_id' in info:
                return self.generate_deid_username_from_user_id(info['user_id'])

        # TODO: what to do if the username isn't found.  Do we delete it?  Leave it?  Stub it?
        return None

    def get_log_string_for_event(self, event):
        # Create a string to use when logging errors, to provide some context.
        event_type = event.get('event_type')
        if isinstance(event_type, str):
            event_type = event_type.decode('utf8')
        event_source = event.get('event_source')
        debug_str = u" [source='{}' type='{}']".format(event_source, event_type)
        return debug_str

    def remap_user_info_in_event(self, event, event_data):
        """
        Harvest user info from event, and remap those values (in place) where appropriate.

        Returns a dict of iterables, with key values of 'username', 'user_id', and 'name'.

        """
        # Find user info, and
        debug_str = self.get_log_string_for_event(event)
        
        # Create a user_info structure to collect relevant user information to look
        # for elsewhere in the event.  We need to return a dictionary of iterables,
        # but since we will potentially be adding the same values repeatedly from
        # different parts of the event, a set will make sure these are deduped.
        user_info = defaultdict(set)

        # user_info_found = []
        # Fetch and then remap username.
        # username_entry = None

        # Note that eventlog.get_event_username() does a strip on the username and checks for zero-len,
        # so we don't have to do so here.
        username = eventlog.get_event_username(event)
        if username is not None:
            username = username.decode('utf8')
            remapped_username = self.remap_username(username, user_info)
            if remapped_username is not None:
                event['username'] = remapped_username
            else:
                # TODO: what to do if the username isn't found.  Do we delete it?  Leave it?  Stub it?
                pass

        # Get the user_id from context, either as an int or None, and remap.
        userid_entry = None
        user_id = self.get_user_id_as_int(event.get('context', {}).get('user_id'))
        if user_id is not None:
            user_info['user_id'].add(user_id)
            info = self.get_user_info_for_user_id(user_id)
            if info is not None:
                for key, value in info.iteritems():
                    user_info[key].add(value)
                if username is not None and 'username' in info and username != info['username']:
                    log.error(
                        u"user_id ('%s'=>'%s') does not match username ('%s') %s",
                        user_id, info['username'], username, debug_str,
                    )
            event['context']['user_id'] = self.remap_id(user_id)

        # Clean username from context.
        if 'context' in event:
            # Remap value of username in context, if it is present.  (Removed in more recent events.)
            if 'username' in event['context'] and len(event['context']['username'].strip()) > 0:
                context_username = event['context']['username'].strip().decode('utf8')
                remapped_username = self.remap_username(context_username, user_info)
                if remapped_username is not None:
                    event['context']['username'] = remapped_username
                else:
                    # TODO: what to do if the username isn't found.  Do we delete it?  Leave it?  Stub it?
                    pass

        # Look into the event payload.
        if event_data:
            # Get the user_id from payload and remap.
            event_userid_entry = None
            event_user_id = self.get_user_id_as_int(event_data.get('user_id'))
            if event_user_id is not None:
                user_info['user_id'].add(event_user_id)
                info = self.get_user_info_for_user_id(event_user_id)
                if info is not None:
                    for key, value in info.iteritems():
                        user_info[key].add(value)
                event_data['user_id'] = self.remap_id(event_user_id)

            # Remap values of usernames in payload, if present.  Usernames may appear with different key values.
            # TODO: confirm that these values are usernames, not user_id values.
            # TODO: decide how to handle requesting_student_id (on openassessmentblock.get_peer_submission events).
            for username_key in ['username', 'instructor', 'student', 'user']:
                if username_key in event_data and len(event_data[username_key].strip()) > 0:
                    event_username = event_data[username_key].strip().decode('utf8')
                    remapped_username = self.remap_username(event_username, user_info)
                    if remapped_username is not None:
                        event_data[username_key] = remapped_username
                    else:
                        # TODO: what to do if the username isn't found.  Do we delete it?  Leave it?  Stub it?
                        pass

        # Finally return the fully-constructed dict.
        return user_info

    def deidentify_event(self, event):
        """Deidentify event by removing/stubbing user information."""

        # Create a string to use when logging errors, to provide some context.
        debug_str = self.get_log_string_for_event(event)        

        # Remap the user information stored in the event, and collect for later searching.
        event_data = eventlog.get_event_data(event)
        user_info = self.remap_user_info_in_event(event, event_data)
        
        # Clean or remove values from context.
        if 'context' in event:
            # These aren't present in current events, but are generated historically by some implicit events.
            # TODO: should these be removed, or set to ''?
            event['context'].pop('host', None)
            event['context'].pop('ip', None)

            # Clean event.context.path.
            if 'path' in event['context']:
                updated_context_path = self.deidentifier.deidentify_structure(event['context']['path'], u"context.path", user_info)
                if updated_context_path is not None:
                    event['context']['path'] = updated_context_path
                    if self.deidentifier.is_logging_enabled():
                        log.info(u"Deidentified event.context.path %s", debug_str)

            # TODO: check event.context.client.ip
            # TODO: check event.context.client.device.id
            # TODO: check event.context.client.device.userid

        # Do remaining cleanup on the entire event payload (assuming user-based remapping has been done).
        if event_data:
            # Remove sensitive payload fields.
            # TODO: decide how to handle 'url' and 'report_url'.
            for key in ['certificate_id', 'certificate_url', 'source_url', 'fileName', 'GET', 'POST']:
                event_data.pop(key, None)

            for key in ['answer', 'saved_response']:
                if key in event_data and 'file_upload_key' in event_data[key]:
                    event_data[key].pop('file_upload_key')

            # Clean up remaining event payload recursively.
            updated_event_data = self.deidentifier.deidentify_structure(event_data, u"event", user_info)
            if updated_event_data is not None:
                if self.deidentifier.is_logging_enabled():
                    log.info(u"Deidentified payload: %s", debug_str)
                event_data = updated_event_data

            # Re-encode payload as a json string if it originally was one.
            # (This test works because we throw away string values that didn't parse as JSON.)
            if isinstance(event.get('event'), basestring):
                event['event'] = cjson.encode(event_data)
            else:
                event['event'] = event_data

        # Delete or clean base properties other than username.
        event.pop('host', None)  # delete host
        event.pop('ip', None)  # delete ip

        # Clean page and referer
        for key in ['page', 'referer']:
            if key in event:
                label = u"event.{}".format(key)
                updated_value = self.deidentifier.deidentify_structure(event[key], label, user_info)
                if updated_value is not None:
                    event[key] = updated_value
                    if self.deidentifier.is_logging_enabled():
                        log.info(u"Deidentified %s %s", label, debug_str)

        return event

    def extra_modules(self):
        import numpy
        return [numpy]


class EventDeidentificationTask(DeidentifierParamsMixin, UserInfoDownstreamMixin, MapReduceJobTaskMixin, luigi.WrapperTask):
    """Wrapper task for course events deidentification."""

    course = luigi.Parameter(is_list=True)
    dump_root = luigi.Parameter()
    output_root = luigi.Parameter()
    explicit_event_whitelist = luigi.Parameter(
        config_path={'section': 'deidentification', 'name': 'explicit_event_whitelist'}
    )

    def requires(self):
        for course in self.course:
            # we already have course events dumped separately, so each DeidentifyCourseEventsTask would have a different source.
            filename_safe_course_id = opaque_key_util.get_filename_safe_course_id(course)
            kwargs = {
                'course': course,
                'dump_root': self.dump_root,
                'output_root': self.output_root,
                'explicit_event_whitelist': self.explicit_event_whitelist,
                'n_reduce_tasks': self.n_reduce_tasks,
                'entities': self.entities,
                'log_context': self.log_context,
                'auth_user_path': self.auth_user_path,
                'auth_userprofile_path': self.auth_userprofile_path,
            }
            yield DeidentifyCourseEventsTask(**kwargs)
