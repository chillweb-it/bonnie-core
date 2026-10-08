import os
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from knowledge_api import create_api
from knowledge_service import chunks, page_id, Scope, KnowledgeService, KnowledgeUnavailable, vector_text, NotionKnowledgeSource


class KnowledgeTests(unittest.TestCase):
    def test_chunk_preserves_heading_and_tail(self):
        text = '# Rules\n' + '欖球剪片' * 500 + '\n最後一句'
        parts = chunks(text)
        self.assertTrue(all(s == 'Rules' for s, _ in parts))
        self.assertTrue(all(len(c) <= 700 for _, c in parts))
        self.assertIn('最後一句', parts[-1][1])
        self.assertEqual(''.join(c for _, c in parts).replace('\n', ''), text.split('\n', 1)[1].replace('\n', ''))

    def test_source_url_validation(self):
        self.assertEqual(page_id('https://www.notion.so/3f38b9d77d6b815088ccc9550f417eaa'), '3f38b9d7-7d6b-8150-88cc-c9550f417eaa')
        with self.assertRaises(ValueError):
            page_id('https://evil.example/3f38b9d77d6b815088ccc9550f417eaa')

    def test_unknown_company_rejected_before_embedding(self):
        class NoEmbedding:
            def embed(self, texts):
                raise AssertionError('Must not embed unauthorized queries')
        svc = KnowledgeService('', None, NoEmbedding())
        svc.ready.set()
        with self.assertRaises(PermissionError):
            svc.search('budget', Scope('staff'), company='Cloud Decoct')

    def test_dimension_guard(self):
        with self.assertRaises(ValueError):
            vector_text([0.1] * 3)

    def test_inactive_and_unapproved_sources_never_fetched(self):
        def select(name):
            return {'type': 'select', 'select': {'name': name}}
        class Client:
            def _query(self, source, body):
                return {'results': [{'properties': {'Status': select('Draft'), 'Authority': select('Approved')}}, {'properties': {'Status': select('Active'), 'Authority': select('Draft')}}]}
            def get_page(self, source):
                raise AssertionError('Do not fetch unapproved content')
        self.assertEqual(NotionKnowledgeSource(Client(), 'registry').documents(), [])

    def test_api_requires_token_and_keeps_server_scope(self):
        class Service:
            import threading
            ready = threading.Event()
            def search(self, query, scope, **kw):
                if kw['company'] and kw['company'] not in scope.companies:
                    raise PermissionError()
                return {'principal': scope.principal, 'companies': scope.companies}
        with patch.dict(os.environ, {'KNOWLEDGE_API_KEYS': '{"test-key":{"principal":"Hayley","companies":["Cloud Decoct"]}}'}):
            api = TestClient(create_api(Service()))
            self.assertEqual(api.post('/v1/knowledge/search', json={'query':'rules'}).status_code,401)
            headers = {'Authorization':'Bearer test-key'}
            self.assertEqual(api.post('/v1/knowledge/search',json={'query':'rules','company':'ChillWeb'},headers=headers).status_code,403)
            response=api.post('/v1/knowledge/search',json={'query':'rules','company':'Cloud Decoct','principal':'CEO'},headers=headers)
            self.assertEqual(response.json()['principal'], 'Hayley')
            self.assertEqual(api.get('/v1/system/runtime',headers=headers).status_code,403)
            response=api.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/list'},headers=headers)
            self.assertEqual(response.json()['result']['tools'][0]['name'],'knowledge_search')

    def test_missing_required_knowledge_fails_closed(self):
        svc = KnowledgeService('',None)
        with self.assertRaises(KnowledgeUnavailable):
            svc.search('rules',Scope('staff'))


if __name__ == '__main__':
    unittest.main()
