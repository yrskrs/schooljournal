from django.test import TestCase

from apps.journals.models import (ServiceConnection, GradeAPIConnection, Grade, ExternalGrade,
    GradeStudentMapping, GradePullLog, ExtraStudent, APIInboxResult, DeletedAPIIdentity)
from apps.journals.services.rebuild_connections import recreate_bindings
from tests import test_school_api_v2 as fixtures


class RebuildConnectionsTests(TestCase):
    setUp = fixtures.SchoolAPIV2Tests.setUp
    row = fixtures.SchoolAPIV2Tests.row
    pull = fixtures.SchoolAPIV2Tests.pull

    def test_replace_old_ids_keeps_grades_history_extras_and_only_one_working_context(self):
        old = ServiceConnection.objects.create(owner=self.teacher, source='schooltest', name='Old',
            identity_key='old', token='old-secret')
        self.connection.service_connection = old
        self.connection.save()
        self.pull(self.row())
        record = ExternalGrade.objects.get()
        grade = Grade.objects.get()
        second = GradeAPIConnection.objects.create(journal=self.journal, service_connection=old,
            source='schooltest', contract_version=2, source_class_id='7', source_subject_id='3',
            name='Duplicate', url=self.connection.url, api_key='old-secret')
        duplicate = ExternalGrade.objects.create(connection=second, external_id=record.external_id,
            student=record.student, lesson=record.lesson, value=record.value, fingerprint=record.fingerprint,
            status=ExternalGrade.Status.CONFLICT)
        extra = ExtraStudent.objects.create(connection=second, external_key='outside:1', name='Synthetic')
        inbox = APIInboxResult.objects.create(connection=second, external_id='outside-result', extra_student=extra)
        tombstone = DeletedAPIIdentity.objects.create(connection=second, external_key='deleted:2')
        log = GradePullLog.objects.create(connection=second, user=self.teacher, counts={'errors': 1})
        new = ServiceConnection.objects.create(owner=self.teacher, source='schooltest', name='New',
            identity_key='new', token='new-secret')
        old_ids = [self.connection.pk, second.pk]
        result = recreate_bindings(self.teacher, {'schooltest': new})
        self.assertFalse(GradeAPIConnection.objects.filter(pk__in=old_ids).exists())
        self.assertEqual(len(result['active']), 1)
        self.assertEqual(GradeAPIConnection.objects.filter(enabled=True).count(), 1)
        grade.refresh_from_db()
        self.assertEqual((grade.pk, grade.value, grade.import_record_id), (grade.pk, '8', record.pk))
        self.assertEqual(ExternalGrade.objects.count(), 2)
        for row in [record, duplicate, extra, inbox, tombstone, log]:
            row.refresh_from_db()
            self.assertIn(row.connection_id, result['replacements'].values())
        self.assertEqual(duplicate.status, ExternalGrade.Status.CONFLICT)
        self.assertEqual(inbox.extra_student_id, extra.pk)
        for binding in GradeAPIConnection.objects.all():
            self.assertEqual(binding.api_key, 'new-secret')
            self.assertEqual(binding.service_connection_id, new.pk)

    def test_missing_source_rolls_back_without_deleting_any_binding(self):
        service = ServiceConnection.objects.create(owner=self.teacher, source='schooltest', name='Old',
            identity_key='old', token='old-secret')
        self.connection.service_connection = service; self.connection.save()
        with self.assertRaises(ValueError):
            recreate_bindings(self.teacher, {})
        self.assertTrue(GradeAPIConnection.objects.filter(pk=self.connection.pk).exists())
