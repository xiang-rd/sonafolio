import unittest

import numpy as np

from sonafolio.ctc import align_tokens
from sonafolio.refinement import refine_segment


class RefinementTests(unittest.TestCase):
    def test_ctc_keeps_repeated_characters_separated_by_blank(self):
        probabilities = np.array([[.99,.01],[.01,.99],[.99,.01],[.01,.99],[.99,.01]])
        result = align_tokens(np.log(probabilities), [1,1], 0)
        self.assertIsNotNone(result)
        self.assertLessEqual(result[0]['last_frame'],result[1]['first_frame'])

    def test_ctc_can_skip_leading_silence(self):
        probabilities = np.array([[.99,.005,.005],[.99,.005,.005],[.01,.98,.01],[.99,.005,.005],[.01,.01,.98]])
        result = align_tokens(np.log(probabilities), [1,2], 0)
        self.assertEqual(result[0]['first_frame'],2)
        self.assertEqual(result[1]['first_frame'],4)

    def test_refinement_preserves_all_characters_and_speaker_change(self):
        segment={'id':'seg-1','text':'甲？B乙。','start':0,'end':3,'speaker_id':None}
        timings=[{'start':.1,'end':.5,'score':.9},None,None,{'start':2,'end':2.5,'score':.9},None]
        turns=[{'start':0,'end':1,'speaker_id':'A'},{'start':1.5,'end':3,'speaker_id':'B'}]
        result=refine_segment(segment,timings,turns)
        self.assertEqual(''.join(x['text'] for x in result),segment['text'])
        self.assertEqual(result[0]['speaker_id'],'A')
        self.assertIsNone(result[1]['speaker_id'])
        self.assertEqual(result[2]['speaker_id'],'B')

    def test_bad_alignment_does_not_invent_times(self):
        segment={'id':'s','text':'你好','start':0,'end':2,'speaker_id':None}
        result=refine_segment(segment,[None,None],[])
        self.assertIsNone(result[0]['speaker_id'])
        self.assertEqual(result[0]['text'],'你好')


if __name__=='__main__':
    unittest.main()
