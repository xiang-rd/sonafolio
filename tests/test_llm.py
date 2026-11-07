"""Validate the API contract, bounded retries and secret handling without network."""

import json
from typing import Literal
import unittest

import httpx
from pydantic import BaseModel

from sonafolio.llm import LLMSettings, request_json


class Answer(BaseModel):
    ok: Literal[True]


class LLMTests(unittest.TestCase):
    def test_json_contract_and_usage(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://api.deepseek.com/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer test-key")
            body = json.loads(request.content)
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.assertEqual(body["thinking"], {"type": "disabled"})
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}], "usage": {"total_tokens": 10}})
        result, metadata = request_json("Return JSON", "test", Answer, api_key="test-key", settings=LLMSettings(), transport=httpx.MockTransport(handler))
        self.assertTrue(result.ok)
        self.assertEqual(metadata["usage"]["total_tokens"], 10)

    def test_schema_failure_is_retried_only_once(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]})
        with self.assertRaisesRegex(RuntimeError, "two attempts"):
            request_json("Return JSON", "test", Answer, api_key="test-key", settings=LLMSettings(), transport=httpx.MockTransport(handler))
        self.assertEqual(len(calls), 2)

    def test_auth_failure_does_not_expose_server_body_or_retry(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(401, text="sensitive-server-body-and-key")
        with self.assertRaises(RuntimeError) as caught:
            request_json("Return JSON", "test", Answer, api_key="test-key", settings=LLMSettings(), transport=httpx.MockTransport(handler))
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertNotIn("test-key", str(caught.exception))
        self.assertEqual(len(calls), 1)

    def test_truncation_is_not_accepted_as_success(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": '{"ok":true}'}}]}))
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            request_json("Return JSON", "test", Answer, api_key="test-key", settings=LLMSettings(), transport=transport)

    def test_repair_feedback_includes_all_schema_errors(self):
        calls=[]
        schema={'type':'object','additionalProperties':False,'properties':{'ids':{'type':'array','items':{'type':'string'},'maxItems':1}},'required':['ids']}
        def handler(request):
            body=json.loads(request.content); calls.append(body)
            if len(calls)==1:
                content=json.dumps({'ids':['a','b'],'unexpected':True})
            else:
                self.assertIn('unexpected',body['messages'][-1]['content'])
                self.assertIn('too long',body['messages'][-1]['content'])
                content=json.dumps({'ids':['a']})
            return httpx.Response(200,json={'choices':[{'message':{'content':content},'finish_reason':'stop'}]})
        result,_=request_json('Return JSON','test',schema,api_key='test-key',settings=LLMSettings(),transport=httpx.MockTransport(handler))
        self.assertEqual(result,{'ids':['a']})
        self.assertEqual(len(calls),2)


if __name__ == "__main__":
    unittest.main()
