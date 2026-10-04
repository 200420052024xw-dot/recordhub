"""Regression coverage for the upstream architecture + local Skill integration."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from fastapi.testclient import TestClient
from config import AppSettings, DeepSeekSettings, FeishuSettings, load_schedule_config, load_table_config
from data import FileStateStore
from analysis.materials import MaterialPreparer
from analysis.models import AnalysisRequest
from analysis.repositories import AnalysisRepository, SkillConfigRepository
from analysis.store import AnalysisStore
from analysis.workflow import AnalysisWorkflow
from llm import LLMError, PromptService
from schema import LogResource, TableConfig
from service.api import _build_scheduler, create_app
from service.runtime import Runtime, WorkflowBinding, build_runtime
from skills import MaterialScope, SkillCode, SkillConfig, SkillInput, SkillRunner, SkillStatus, TextRecord
from workflow1.models import DailySnapshot, UnitStatus
from test_workflow1_pipeline import DAY, sample_snapshot


class MemoryBitable:
    app_token = 'test-base'

    def __init__(self):
        self.records = {'department': [], 'team': []}
        self.creates = self.updates = 0
        self.lose_create_response = False

    def list_fields(self, table_id):
        names = ['文本', '进展', '人员', 'idea', '谈话'] if table_id == 'department' else ['文本', '技术', '培训']
        return [{'field_name': name, 'type': 1} for name in names]

    def list_records(self, table_id):
        return self.records.get(table_id, [])

    def create_record(self, table_id, fields):
        self.creates += 1
        record = {'record_id': f'rec-{self.creates}', 'fields': dict(fields)}
        self.records[table_id].append(record)
        if self.lose_create_response:
            self.lose_create_response = False
            raise ConnectionError('created but response lost')
        return record

    def update_record(self, table_id, record_id, fields):
        self.updates += 1
        for record in self.records[table_id]:
            if record['record_id'] == record_id:
                record['fields'].update(fields)
                return record
        raise LookupError(record_id)


def tables():
    return TableConfig.model_validate({'tables': {
        'department_analysis': {'table_id': 'department', 'fields': {
            'title': '文本', 'progress_s04': '进展', 'personnel_s05': '人员',
            'ideas_s06': 'idea', 'meeting_focus_s07': '谈话'}},
        'team_analysis': {'table_id': 'team', 'fields': {
            'title': '文本', 'technology_s08': '技术', 'training_s09': '培训'}},
    }})


def settings(**kwargs):
    return AppSettings(feishu=FeishuSettings('app', 'secret', 'base'),
        deepseek=DeepSeekSettings('key'), admin_token='test-admin',
        archive_parent_folder_token='test-folder', event_stream_enabled=False,
        schedule_config_path=str(ROOT / 'config/schedules.toml'), **kwargs)


class MergeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.daily = FileStateStore(self.temp.name, snapshot_model=DailySnapshot)
        self.daily.get_or_create_workflow(DAY)
        self.snapshot = sample_snapshot()
        self.snapshot.workflow_run_id = self.daily.load_workflow(DAY).workflow_run_id
        for state in self.snapshot.log_evaluations.values():
            state.status = UnitStatus.CONFIRMED
            state.positive_ai = state.positive_final = '完成检查'
            state.improvement_ai = state.improvement_final = '补齐材料'
        self.daily.save_snapshot(self.snapshot)
        self.organization = Mock()
        self.organization.get.return_value = self.snapshot.organization
        self.bitable = MemoryBitable()
        self.client = Mock()
        self.client.complete_json.side_effect = lambda messages, validator: validator({'items': []})
        self.messages = Mock()
        self.messages.send_text.return_value = {'message_id': 'mock-message'}
        self.analysis = AnalysisWorkflow(store=AnalysisStore(self.temp.name),
            preparer=MaterialPreparer(self.daily), organization=self.organization,
            configs=SkillConfigRepository(self.bitable, tables(),
                str(Path(self.temp.name) / 'configs.json'), 'TEAM_MANAGEMENT'),
            repository=AnalysisRepository(self.bitable, tables()),
            prompt_service=PromptService(self.client, max_attempts=1), messages=self.messages)

    def request(self, code='S04', user='D'):
        return AnalysisRequest(skill_code=code, start_date=DAY, end_date=DAY,
            user_id=user, department_ids=['dep'])

    def material(self):
        return SkillInput(run_id='skill-test',
            scope=MaterialScope(start_date=DAY, end_date=DAY, department_ids=['dep']),
            records=[TextRecord(record_id='rec1', person_id='M', name='成员',
                department_id='dep', role='基层学生', work_start=DAY, work_end=DAY,
                confirmed=True, progress='完成检查')])

    def test_all_nine_skills_use_shared_template_api_and_json_models(self):
        responses = {'S01': {'progress': '完成检查'},
            'S02': {'evaluations': [{'record_id': 'rec1', 'positive': '完成检查', 'improvement': '补齐材料'}], 'summary': '检查完成'},
            'S03': {'summary': '检查完成'}}
        for code in SkillCode:
            with self.subTest(code=code):
                self.client.complete_json.side_effect = lambda messages, validator, code=code: validator(responses.get(code.value, {'items': []}))
                result = SkillRunner(PromptService(self.client, max_attempts=1)).run(
                    code, self.material(), department_id='dep', user_id='D')
                self.assertEqual(result.status, SkillStatus.WAITING_CONFIRMATION)
                self.assertEqual(json.loads(result.model_dump_json())['skill_code'], code.value)
                self.assertIn('输入', self.client.complete_json.call_args.args[0][0]['content'])

    def test_missing_or_unconfirmed_input_never_calls_model(self):
        runner = SkillRunner(PromptService(self.client, max_attempts=1))
        data = self.material()
        data.records[0].confirmed = False
        self.assertEqual(runner.run('S04', data, department_id='dep', user_id='D').status, SkillStatus.BLOCKED)
        data.records = []
        self.assertEqual(runner.run('S04', data, department_id='dep', user_id='D').status, SkillStatus.NO_MATERIAL)
        self.client.complete_json.assert_not_called()

    def test_hallucinated_sources_are_rejected(self):
        self.client.complete_json.side_effect = lambda messages, validator: validator({'items': [{
            'source_record_ids': ['outside'], 'work_item': '工作', 'department_ids': ['dep'],
            'participant_ids': ['M'], 'current_progress': '完成'}]})
        with self.assertRaises(LLMError):
            SkillRunner(PromptService(self.client, max_attempts=1)).run(
                'S04', self.material(), department_id='dep', user_id='D')

    def test_personal_config_wins_and_is_frozen(self):
        department = SkillConfig(config_id='dept', skill_code='S04', department_id='dep', version='1')
        personal = department.model_copy(update={'config_id': 'person', 'user_id': 'D', 'version': '2'})
        runner = SkillRunner(PromptService(self.client), [department, personal])
        personal.instructions = 'mutated'
        config = runner.resolve_config('S04', department_id='dep', user_id='D')
        self.assertEqual(config.config_id, 'person')
        self.assertEqual(config.instructions, '')

    def test_completed_archive_survives_cleanup_and_restores_original_id(self):
        original = self.daily.load_workflow(DAY).workflow_run_id
        self.daily.map_external_record(DAY, 'source', 'logs', 'original-record')
        self.daily.set_status(DAY, 'COMPLETED')
        self.daily.cleanup_completed(2, today=date(2026, 10, 10))
        self.assertFalse((self.daily.runs_dir / f'{DAY}.json').exists())
        prepared = MaterialPreparer(self.daily).prepare(run_id='range', start=DAY, end=DAY, department_ids=['dep'])
        self.assertTrue(prepared.input.scope.complete)
        self.assertEqual(prepared.statistics.submitted_people, 2)
        self.assertEqual(len(prepared.input.records), 3)
        self.assertEqual(self.daily.get_or_create_workflow(DAY).workflow_run_id, original)
        self.assertEqual(self.daily.get_external_record(DAY, 'source')['record_id'], 'original-record')

    def test_legacy_active_and_checked_layouts_are_migrated(self):
        raw = json.loads((self.daily.runs_dir / f'{DAY}.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            FileStateStore._atomic_write(root / 'workflows' / f'{DAY}.json', raw)
            FileStateStore._atomic_write(root / 'checked' / '2026-10-02.json', raw)
            FileStateStore._atomic_write(root / 'cache' / 'organization.json', {'payload': {'saved': True}})
            store = FileStateStore(root, snapshot_model=DailySnapshot)
            self.assertIsNotNone(store.load_check_snapshot(DAY))
            self.assertTrue((store.checked_dir / '2026-10-02.json').exists())
            self.assertEqual(store.load_cache('organization'), {'saved': True})

    def test_workflow_archives_are_isolated(self):
        self.daily.set_status(DAY, 'COMPLETED')
        another = FileStateStore(self.temp.name, workflow_type='workflow2', snapshot_model=DailySnapshot)
        self.assertIsNone(another.load_check_snapshot(DAY))

    def test_old_evaluation_timestamps_are_preserved_without_fabrication(self):
        state = self.snapshot.log_evaluations['L1']
        state.source = 'HUMAN'
        state.evaluated_at = datetime(2026, 10, 4, 1, tzinfo=UTC)
        self.daily.save_snapshot(self.snapshot)
        prepared = MaterialPreparer(self.daily).prepare(run_id='range', start=DAY, end=DAY, department_ids=['dep'])
        record = next(record for record in prepared.input.records if record.record_id == 'rec1')
        self.assertIsNone(record.evaluations[0].evaluated_at)
        self.assertEqual(record.evaluations[1].evaluated_at, state.evaluated_at)

    def test_failed_daily_evaluation_blocks_periodic_analysis(self):
        self.snapshot.log_evaluations['L1'].status = UnitStatus.FAILED
        self.daily.save_snapshot(self.snapshot)
        run = self.analysis.run(self.request())
        self.assertEqual(run.status, 'BLOCKED')
        self.assertIsNone(run.material.statistics)
        self.client.complete_json.assert_not_called()

    def test_all_six_periodic_destinations_including_team_title_fallback(self):
        for code in ['S04', 'S05', 'S06', 'S07', 'S08', 'S09']:
            with self.subTest(code=code):
                run = self.analysis.run(self.request(code, 'T'))
                self.assertEqual(run.status, 'WAITING_CONFIRMATION', run.last_error)
                saved = self.analysis.repository.read(run)
                self.assertEqual(saved['material_statistics']['submitted_people'], 2)
                self.assertEqual(saved['skill_code'], code)
                self.assertEqual(saved['content'], {'items': []})
        self.assertEqual(self.bitable.creates, 6)
        self.assertTrue(self.bitable.records['team'][0]['fields']['文本'].startswith('RecordHub:S04:'))

    def test_lost_create_response_recovers_without_repeating_model_or_record(self):
        self.bitable.lose_create_response = True
        first = self.analysis.run(self.request())
        self.assertEqual(first.status, 'FAILED')
        resumed = self.analysis.resume(first.run_id)
        self.assertEqual(resumed.status, 'WAITING_CONFIRMATION', resumed.last_error)
        self.assertEqual(self.bitable.creates, 1)
        self.assertEqual(self.client.complete_json.call_count, 1)
        self.assertEqual(self.messages.send_text.call_count, 1)

    def test_confirmation_preserves_edits_and_draft_and_checks_owner(self):
        run = self.analysis.run(self.request())
        record = self.bitable.records['department'][0]
        edited = json.loads(record['fields']['进展'])
        edited['content']['items'] = [{'source_record_ids': ['rec1'], 'work_item': '检查',
            'department_ids': ['dep'], 'participant_ids': ['M'], 'current_progress': '人工核对完成', 'main_difficulties': ''}]
        record['fields']['进展'] = json.dumps(edited, ensure_ascii=False)
        self.analysis.resume(run.run_id)
        with self.assertRaises(PermissionError):
            self.analysis.confirm(run.run_id, user_id='M')
        confirmed = self.analysis.confirm(run.run_id, user_id='D')
        self.assertEqual(confirmed.status, 'CONFIRMED')
        self.assertEqual(confirmed.draft_result['content'], {'items': []})
        self.assertEqual(confirmed.result['content'], edited['content'])
        self.assertEqual(self.analysis.repository.read(confirmed)['confirmed_by'], 'D')
        self.assertEqual(self.messages.send_text.call_count, 1)

    def test_remote_metadata_tampering_is_rejected(self):
        run = self.analysis.run(self.request())
        record = self.bitable.records['department'][0]
        edited = json.loads(record['fields']['进展'])
        edited['source_record_ids'] = ['outside']
        record['fields']['进展'] = json.dumps(edited)
        with self.assertRaises(ValueError):
            self.analysis.confirm(run.run_id, user_id='D')

    def test_archive_state_schema_still_accepts_resource_metadata(self):
        self.snapshot.logs[0].achievement_refs = ['source-link']
        self.snapshot.logs[0].resources = [LogResource(resource_id='R1', name='课程', category='课程')]
        self.daily.save_snapshot(self.snapshot)
        self.daily.set_status(DAY, 'COMPLETED')
        material = MaterialPreparer(self.daily).prepare(run_id='range', start=DAY, end=DAY, department_ids=['dep'])
        self.assertEqual(material.input.resources[0].source_record_ids, ['rec1'])

    def test_runtime_assembly_uses_registered_daily_store_and_shared_message_override(self):
        infrastructure = Mock()
        with patch('service.runtime._build_infrastructure', return_value=infrastructure):
            runtime = build_runtime(settings(state_dir=self.temp.name, table_config_path=str(ROOT / 'config/tables.toml')))
        self.assertIn('workflow1_daily', runtime.workflows)
        self.assertIs(runtime.analysis.messages, infrastructure.messages)
        self.assertIs(runtime.analysis.preparer.store, runtime.workflows['workflow1_daily'].store)

    def test_analysis_scheduler_is_opt_in_and_keeps_daily_registry(self):
        binding = WorkflowBinding(name='workflow1_daily', workflow=Mock(), store=Mock(),
            auto_advance_at='', confirmation_webhook_token='webhook', record_handlers={})
        schedules = load_schedule_config(ROOT / 'config/schedules.toml')
        for enabled in (False, True):
            configured = settings(analysis_enabled=enabled)
            runtime = Runtime(settings=configured, infrastructure=Mock(), organization_cache=Mock(),
                workflows={binding.name: binding}, analysis=self.analysis)
            jobs = {job.id for job in _build_scheduler(schedules, runtime, configured).get_jobs()}
            self.assertIn('workflow1_daily', jobs)
            self.assertIn('snapshot_cleanup', jobs)
            self.assertEqual('analysis_recovery' in jobs, enabled)
            self.assertEqual('s08_public_technology' in jobs, enabled)

    def test_admin_api_preserves_person_routes_and_authorization(self):
        configured = settings()
        binding = WorkflowBinding(name='workflow1_daily', workflow=Mock(), store=self.daily,
            auto_advance_at='', confirmation_webhook_token='webhook', record_handlers={})
        runtime = Runtime(settings=configured, infrastructure=Mock(), organization_cache=self.organization,
            workflows={binding.name: binding}, analysis=self.analysis)
        client = TestClient(create_app(configured, runtime))
        self.assertIn('/admin/organization/persons', client.app.openapi()['paths'])
        payload = self.request().model_dump(mode='json')
        self.assertEqual(client.post('/admin/analyses', json=payload).status_code, 401)
        headers = {'Authorization': 'Bearer test-admin'}
        created = client.post('/admin/analyses', json=payload, headers=headers)
        self.assertEqual(created.status_code, 200)
        identifier = created.json()['run_id']
        denied = client.post(f'/admin/analyses/{identifier}/confirm', json={'user_id': 'M'}, headers=headers)
        self.assertEqual(denied.status_code, 403)
        confirmed = client.post(f'/admin/analyses/{identifier}/confirm', json={'user_id': 'D'}, headers=headers)
        self.assertEqual(confirmed.json()['status'], 'CONFIRMED')

    def test_mapping_template_requires_real_table_ids(self):
        self.assertIn('department_analysis', load_table_config(ROOT / 'config/tables.toml').tables)
        with self.assertRaisesRegex(ValueError, 'table IDs are empty'):
            load_table_config(ROOT / 'config/tables.example.toml')


if __name__ == '__main__':
    unittest.main()
