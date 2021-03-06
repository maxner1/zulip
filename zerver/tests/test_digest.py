import datetime
import time
from typing import List
from unittest import mock

from django.test import override_settings
from django.utils.timezone import now as timezone_now

from confirmation.models import one_click_unsubscribe_link
from zerver.lib.actions import do_create_user
from zerver.lib.digest import (
    bulk_handle_digest_email,
    enqueue_emails,
    gather_new_streams,
    handle_digest_email,
    streams_recently_modified_for_user,
)
from zerver.lib.streams import create_stream_if_needed
from zerver.lib.test_classes import ZulipTestCase
from zerver.lib.test_helpers import cache_tries_captured, queries_captured
from zerver.models import (
    Message,
    Realm,
    RealmAuditLog,
    UserActivity,
    UserProfile,
    flush_per_request_caches,
    get_client,
    get_realm,
    get_stream,
)


class TestDigestEmailMessages(ZulipTestCase):

    @mock.patch('zerver.lib.digest.enough_traffic')
    @mock.patch('zerver.lib.digest.send_future_email')
    def test_multiple_stream_senders(self,
                                     mock_send_future_email: mock.MagicMock,
                                     mock_enough_traffic: mock.MagicMock) -> None:

        othello = self.example_user('othello')
        self.subscribe(othello, 'Verona')

        one_day_ago = timezone_now() - datetime.timedelta(days=1)
        Message.objects.all().update(date_sent=one_day_ago)
        one_hour_ago = timezone_now() - datetime.timedelta(seconds=3600)

        cutoff = time.mktime(one_hour_ago.timetuple())

        senders = ['hamlet', 'cordelia',  'iago', 'prospero', 'ZOE']
        self.simulate_stream_conversation('Verona', senders)

        flush_per_request_caches()
        # When this test is run in isolation, one additional query is run which
        # is equivalent to
        # ContentType.objects.get(app_label='zerver', model='userprofile')
        # This code is run when we call `confirmation.models.create_confirmation_link`.
        # To trigger this, we call the one_click_unsubscribe_link function below.
        one_click_unsubscribe_link(othello, 'digest')
        with queries_captured() as queries:
            handle_digest_email(othello.id, cutoff)

        self.assert_length(queries, 7)

        self.assertEqual(mock_send_future_email.call_count, 1)
        kwargs = mock_send_future_email.call_args[1]
        self.assertEqual(kwargs['to_user_ids'], [othello.id])

        hot_convo = kwargs['context']['hot_conversations'][0]

        expected_participants = {
            self.example_user(sender).full_name
            for sender in senders
        }

        self.assertEqual(set(hot_convo['participants']), expected_participants)
        self.assertEqual(hot_convo['count'], 5 - 2)  # 5 messages, but 2 shown
        teaser_messages = hot_convo['first_few_messages'][0]['senders']
        self.assertIn('some content', teaser_messages[0]['content'][0]['plain'])
        self.assertIn(teaser_messages[0]['sender'], expected_participants)

    @mock.patch('zerver.lib.digest.enough_traffic')
    @mock.patch('zerver.lib.digest.send_future_email')
    def test_guest_user_multiple_stream_sender(self,
                                               mock_send_future_email: mock.MagicMock,
                                               mock_enough_traffic: mock.MagicMock) -> None:
        othello = self.example_user('othello')
        hamlet = self.example_user('hamlet')
        cordelia = self.example_user('cordelia')
        polonius = self.example_user('polonius')
        create_stream_if_needed(cordelia.realm, 'web_public_stream', is_web_public=True)
        self.subscribe(othello, 'web_public_stream')
        self.subscribe(hamlet, 'web_public_stream')
        self.subscribe(cordelia, 'web_public_stream')
        self.subscribe(polonius, 'web_public_stream')

        one_day_ago = timezone_now() - datetime.timedelta(days=1)
        Message.objects.all().update(date_sent=one_day_ago)
        one_hour_ago = timezone_now() - datetime.timedelta(seconds=3600)

        cutoff = time.mktime(one_hour_ago.timetuple())

        senders = ['hamlet', 'cordelia',  'othello', 'desdemona']
        self.simulate_stream_conversation('web_public_stream', senders)

        flush_per_request_caches()
        # When this test is run in isolation, one additional query is run which
        # is equivalent to
        # ContentType.objects.get(app_label='zerver', model='userprofile')
        # This code is run when we call `confirmation.models.create_confirmation_link`.
        # To trigger this, we call the one_click_unsubscribe_link function below.
        one_click_unsubscribe_link(polonius, 'digest')
        with queries_captured() as queries:
            handle_digest_email(polonius.id, cutoff)

        self.assert_length(queries, 7)

        self.assertEqual(mock_send_future_email.call_count, 1)
        kwargs = mock_send_future_email.call_args[1]
        self.assertEqual(kwargs['to_user_ids'], [polonius.id])

        new_stream_names = kwargs['context']['new_streams']['plain']
        self.assertTrue('web_public_stream' in new_stream_names)

    def test_soft_deactivated_user_multiple_stream_senders(self) -> None:
        one_day_ago = timezone_now() - datetime.timedelta(days=1)
        Message.objects.all().update(date_sent=one_day_ago)

        digest_users = [
            self.example_user('othello'),
            self.example_user('aaron'),
            self.example_user('desdemona'),
            self.example_user('polonius'),
        ]

        for digest_user in digest_users:
            for stream in ['Verona', 'Scotland', 'Denmark']:
                self.subscribe(digest_user, stream)

        RealmAuditLog.objects.all().delete()

        for digest_user in digest_users:
            digest_user.long_term_idle = True
            digest_user.save(update_fields=['long_term_idle'])

        # Send messages to a stream and unsubscribe - subscribe from that stream
        senders = ['hamlet', 'cordelia',  'iago', 'prospero', 'ZOE']
        self.simulate_stream_conversation('Verona', senders)

        for digest_user in digest_users:
            self.unsubscribe(digest_user, 'Verona')
            self.subscribe(digest_user, 'Verona')

        # Send messages to other streams
        self.simulate_stream_conversation('Scotland', senders)
        self.simulate_stream_conversation('Denmark', senders)

        one_hour_ago = timezone_now() - datetime.timedelta(seconds=3600)
        cutoff = time.mktime(one_hour_ago.timetuple())

        flush_per_request_caches()

        # When this test is run in isolation, one additional query is run which
        # is equivalent to
        # ContentType.objects.get(app_label='zerver', model='userprofile')
        # This code is run when we call `confirmation.models.create_confirmation_link`.
        # To trigger this, we call the one_click_unsubscribe_link function below.
        one_click_unsubscribe_link(digest_users[0], 'digest')

        with mock.patch('zerver.lib.digest.send_future_email') as mock_send_future_email:
            digest_user_ids = [user.id for user in digest_users]

            with queries_captured() as queries:
                with cache_tries_captured() as cache_tries:
                    bulk_handle_digest_email(digest_user_ids, cutoff)

            self.assert_length(queries, 37)
            self.assert_length(cache_tries, 4)

        self.assertEqual(mock_send_future_email.call_count, len(digest_users))

        for i, digest_user in enumerate(digest_users):
            kwargs = mock_send_future_email.call_args_list[i][1]
            self.assertEqual(kwargs['to_user_ids'], [digest_user.id])

            hot_conversations = kwargs['context']['hot_conversations']
            self.assertEqual(2, len(hot_conversations), [digest_user.id])

            hot_convo = hot_conversations[0]
            expected_participants = {
                self.example_user(sender).full_name
                for sender in senders
            }

            self.assertEqual(set(hot_convo['participants']), expected_participants)
            self.assertEqual(hot_convo['count'], 5 - 2)  # 5 messages, but 2 shown
            teaser_messages = hot_convo['first_few_messages'][0]['senders']
            self.assertIn('some content', teaser_messages[0]['content'][0]['plain'])
            self.assertIn(teaser_messages[0]['sender'], expected_participants)

    def test_streams_recently_modified_for_user(self) -> None:
        othello = self.example_user('othello')
        cordelia = self.example_user('cordelia')

        for stream in ['Verona', 'Scotland', 'Denmark']:
            self.subscribe(othello, stream)
            self.subscribe(cordelia, stream)

        realm = othello.realm
        denmark = get_stream('Denmark', realm)
        verona = get_stream('Verona', realm)

        two_hours_ago = timezone_now() - datetime.timedelta(hours=2)
        one_hour_ago = timezone_now() - datetime.timedelta(hours=1)

        # Delete all RealmAuditLogs to start with a clean slate.
        RealmAuditLog.objects.all().delete()

        # Unsubscribe and subscribe Othello from a stream
        self.unsubscribe(othello, 'Denmark')
        self.subscribe(othello, 'Denmark')

        self.assertEqual(
            streams_recently_modified_for_user(othello, one_hour_ago),
            {denmark.id}
        )

        # Backdate all our logs (so that Denmark will no longer
        # appear like a recently modified stream for Othello).
        RealmAuditLog.objects.all().update(event_time=two_hours_ago)

        # Now Denmark no longer appears recent to Othello.
        self.assertEqual(
            streams_recently_modified_for_user(othello, one_hour_ago),
            set()
        )

        # Unsubscribe and subscribe from a stream
        self.unsubscribe(othello, 'Verona')
        self.subscribe(othello, 'Verona')

        # Now, Verona, but not Denmark, appears recent.
        self.assertEqual(
            streams_recently_modified_for_user(othello, one_hour_ago),
            {verona.id},
        )

        # make sure we don't mix up Othello and Cordelia
        self.unsubscribe(cordelia, 'Denmark')
        self.subscribe(cordelia, 'Denmark')

        self.assertEqual(
            streams_recently_modified_for_user(cordelia, one_hour_ago),
            {denmark.id}
        )

    @mock.patch('zerver.lib.digest.queue_digest_recipient')
    @mock.patch('zerver.lib.digest.timezone_now')
    @override_settings(SEND_DIGEST_EMAILS=True)
    def test_inactive_users_queued_for_digest(self, mock_django_timezone: mock.MagicMock,
                                              mock_queue_digest_recipient: mock.MagicMock) -> None:
        # Turn on realm digest emails for all realms
        Realm.objects.update(digest_emails_enabled=True)
        cutoff = timezone_now()
        # Test Tuesday
        mock_django_timezone.return_value = datetime.datetime(year=2016, month=1, day=5)
        all_user_profiles = UserProfile.objects.filter(
            is_active=True, is_bot=False, enable_digest_emails=True)
        # Check that all users without an a UserActivity entry are considered
        # inactive users and get enqueued.
        enqueue_emails(cutoff)
        self.assertEqual(mock_queue_digest_recipient.call_count, all_user_profiles.count())
        mock_queue_digest_recipient.reset_mock()
        for realm in Realm.objects.filter(deactivated=False, digest_emails_enabled=True):
            user_profiles = all_user_profiles.filter(realm=realm)
            for user_profile in user_profiles:
                UserActivity.objects.create(
                    last_visit=cutoff - datetime.timedelta(days=1),
                    user_profile=user_profile,
                    count=0,
                    client=get_client('test_client'))
        # Check that inactive users are enqueued
        enqueue_emails(cutoff)
        self.assertEqual(mock_queue_digest_recipient.call_count, all_user_profiles.count())

    @mock.patch('zerver.lib.digest.queue_digest_recipient')
    @mock.patch('zerver.lib.digest.timezone_now')
    def test_disabled(self, mock_django_timezone: mock.MagicMock,
                      mock_queue_digest_recipient: mock.MagicMock) -> None:
        cutoff = timezone_now()
        # A Tuesday
        mock_django_timezone.return_value = datetime.datetime(year=2016, month=1, day=5)
        enqueue_emails(cutoff)
        mock_queue_digest_recipient.assert_not_called()

    @mock.patch('zerver.lib.digest.enough_traffic', return_value=True)
    @mock.patch('zerver.lib.digest.timezone_now')
    @override_settings(SEND_DIGEST_EMAILS=True)
    def test_active_users_not_enqueued(self, mock_django_timezone: mock.MagicMock,
                                       mock_enough_traffic: mock.MagicMock) -> None:
        # Turn on realm digest emails for all realms
        Realm.objects.update(digest_emails_enabled=True)
        cutoff = timezone_now()
        # A Tuesday
        mock_django_timezone.return_value = datetime.datetime(year=2016, month=1, day=5)
        realms = Realm.objects.filter(deactivated=False, digest_emails_enabled=True)
        for realm in realms:
            user_profiles = UserProfile.objects.filter(realm=realm)
            for counter, user_profile in enumerate(user_profiles, 1):
                UserActivity.objects.create(
                    last_visit=cutoff + datetime.timedelta(days=1),
                    user_profile=user_profile,
                    count=0,
                    client=get_client('test_client'))
        # Check that an active user is not enqueued
        with mock.patch('zerver.lib.digest.queue_digest_recipient') as mock_queue_digest_recipient:
            enqueue_emails(cutoff)
            self.assertEqual(mock_queue_digest_recipient.call_count, 0)

    @mock.patch('zerver.lib.digest.queue_digest_recipient')
    @mock.patch('zerver.lib.digest.timezone_now')
    @override_settings(SEND_DIGEST_EMAILS=True)
    def test_only_enqueue_on_valid_day(self, mock_django_timezone: mock.MagicMock,
                                       mock_queue_digest_recipient: mock.MagicMock) -> None:
        # Not a Tuesday
        mock_django_timezone.return_value = datetime.datetime(year=2016, month=1, day=6)

        # Check that digests are not sent on days other than Tuesday.
        cutoff = timezone_now()
        enqueue_emails(cutoff)
        self.assertEqual(mock_queue_digest_recipient.call_count, 0)

    @mock.patch('zerver.lib.digest.queue_digest_recipient')
    @mock.patch('zerver.lib.digest.timezone_now')
    @override_settings(SEND_DIGEST_EMAILS=True)
    def test_no_email_digest_for_bots(self, mock_django_timezone: mock.MagicMock,
                                      mock_queue_digest_recipient: mock.MagicMock) -> None:
        # Turn on realm digest emails for all realms
        Realm.objects.update(digest_emails_enabled=True)
        cutoff = timezone_now()
        # A Tuesday
        mock_django_timezone.return_value = datetime.datetime(year=2016, month=1, day=5)
        bot = do_create_user(
            'some_bot@example.com',
            'password',
            get_realm('zulip'),
            'some_bot',
            bot_type=UserProfile.DEFAULT_BOT,
        )
        UserActivity.objects.create(
            last_visit=cutoff - datetime.timedelta(days=1),
            user_profile=bot,
            count=0,
            client=get_client('test_client'))

        # Check that bots are not sent emails
        enqueue_emails(cutoff)
        for arg in mock_queue_digest_recipient.call_args_list:
            user = arg[0][0]
            self.assertNotEqual(user.id, bot.id)

    @mock.patch('zerver.lib.digest.timezone_now')
    @override_settings(SEND_DIGEST_EMAILS=True)
    def test_new_stream_link(self, mock_django_timezone: mock.MagicMock) -> None:
        cutoff = datetime.datetime(year=2017, month=11, day=1, tzinfo=datetime.timezone.utc)
        mock_django_timezone.return_value = datetime.datetime(year=2017, month=11, day=5, tzinfo=datetime.timezone.utc)
        cordelia = self.example_user('cordelia')
        stream_id = create_stream_if_needed(cordelia.realm, 'New stream')[0].id
        new_stream = gather_new_streams(cordelia, cutoff)[1]
        expected_html = f"<a href='http://zulip.testserver/#narrow/stream/{stream_id}-New-stream'>New stream</a>"
        self.assertIn(expected_html, new_stream['html'])

    def simulate_stream_conversation(self, stream: str, senders: List[str]) -> List[int]:
        client = 'website'  # this makes `sent_by_human` return True
        sending_client = get_client(client)
        message_ids = []  # List[int]
        for sender_name in senders:
            sender = self.example_user(sender_name)
            content = f'some content for {stream} from {sender_name}'
            message_id = self.send_stream_message(sender, stream, content)
            message_ids.append(message_id)
        Message.objects.filter(id__in=message_ids).update(sending_client=sending_client)
        return message_ids

class TestDigestContentInBrowser(ZulipTestCase):
    def test_get_digest_content_in_browser(self) -> None:
        self.login('hamlet')
        result = self.client_get("/digest/")
        self.assert_in_success_response(["Click here to log in to Zulip and catch up."], result)
