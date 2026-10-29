"""Independent component-balance and feasibility checks for the shared calculator."""
import math
import unittest
from copy import deepcopy

from picklist_core import calculate_mixing_volumes


DEFAULT = dict(staple=[200000,1,0,0], number_of_staples=200,
               scaffold=[400,.1,0,0], te_10x=[0,10,0,0],
               magnesium=[0,0,100,0], sodium=[0,0,0,100], desired=[100,50,10,1,12,5])


class MixingFeasibilityTests(unittest.TestCase):
    def balanced(self, inputs):
        recipe = calculate_mixing_volumes(**inputs)
        volumes = [row['Volume_uL'] for row in recipe]
        self.assertTrue(all(math.isfinite(v) and v >= 0 for v in volumes))
        self.assertAlmostEqual(sum(volumes), inputs['desired'][0])
        stocks = [inputs[k] for k in ('staple','scaffold','te_10x','magnesium','sodium')]
        for component in range(1,4):
            actual = sum(v*stock[component] for v,stock in zip(volumes,stocks)) / sum(volumes)
            self.assertAlmostEqual(actual, inputs['desired'][component+2])
        self.assertAlmostEqual(volumes[1]*inputs['scaffold'][0]/sum(volumes), inputs['desired'][2])
        return volumes

    def test_nonunit_te_and_buffer_carryover(self):
        inputs = deepcopy(DEFAULT)
        inputs.update(staple=[200000,1,20,40], scaffold=[400,.1,8,12])
        inputs['desired'][3] = 2
        volumes = self.balanced(inputs)
        self.assertAlmostEqual(volumes[2], (200 - 5 - .25)/10)
        self.assertAlmostEqual(volumes[3], (1200 - 5*20 - 2.5*8)/100)
        self.assertAlmostEqual(volumes[4], (500 - 5*40 - 2.5*12)/100)

    def test_all_buffer_stocks_are_balanced_jointly(self):
        inputs = deepcopy(DEFAULT)
        inputs.update(staple=[200000,1,2,3], scaffold=[400,.1,4,5],
                      te_10x=[0,10,2,3], magnesium=[0,1,100,4], sodium=[0,2,5,100])
        # Construct a feasible target from independently chosen additions.
        expected = [5,2.5,6,7,8]
        stocks = [inputs[k] for k in ('staple','scaffold','te_10x','magnesium','sodium')]
        inputs['desired'][3:] = [sum(v*s[c] for v,s in zip(expected,stocks))/100 for c in (1,2,3)]
        for actual, wanted in zip(self.balanced(inputs), expected):
            self.assertAlmostEqual(actual,wanted)

    def test_zero_targets_and_absent_stocks(self):
        inputs = deepcopy(DEFAULT)
        inputs.update(staple=[200000,0,0,0], scaffold=[0,0,0,0],
                      te_10x=[0,0,0,0], magnesium=[0,0,0,0], sodium=[0,0,0,0])
        inputs['desired'][2:] = [0,0,0,0]
        self.assertEqual(self.balanced(inputs), [5,0,0,0,0,95])

    def test_redundant_stocks_choose_feasible_minimum_volume(self):
        inputs = deepcopy(DEFAULT)
        inputs.update(staple=[200000,0,0,0], scaffold=[0,0,0,0],
                      te_10x=[0,1,0,0], magnesium=[0,10,0,0], sodium=[0,0,0,0])
        inputs['desired'][2:] = [0,2,0,0]
        self.assertEqual(self.balanced(inputs), [5,0,0,20,0,75])

    def test_excess_carryover_rejected(self):
        for component, label in ((1,'TE'),(2,'Mg'),(3,'Na')):
            inputs = deepcopy(DEFAULT)
            inputs['staple'][component] = 1000
            with self.subTest(component=label), self.assertRaisesRegex(ValueError,label+' carried'):
                calculate_mixing_volumes(**inputs)

    def test_insufficiently_concentrated_stocks_rejected(self):
        inputs = deepcopy(DEFAULT); inputs['magnesium'][2] = 1
        with self.assertRaisesRegex(ValueError,'negative water'):
            calculate_mixing_volumes(**inputs)
        inputs = deepcopy(DEFAULT); inputs['scaffold'][0] = 1
        with self.assertRaisesRegex(ValueError,'exceeding'):
            calculate_mixing_volumes(**inputs)

    def test_cross_stock_ratio_cannot_reach_targets(self):
        inputs = deepcopy(DEFAULT)
        inputs['magnesium'][3] = 100  # Adding enough Mg necessarily overshoots Na.
        with self.assertRaisesRegex(ValueError,'nonnegative additions'):
            calculate_mixing_volumes(**inputs)

    def test_missing_stock_and_invalid_numbers_rejected(self):
        for key,index,value in [('te_10x',1,0),('scaffold',0,0),('staple',0,0),
                                ('desired',0,0),('desired',3,-1),('magnesium',2,float('nan')),
                                ('sodium',3,float('inf')),('te_10x',0,1)]:
            inputs = deepcopy(DEFAULT); inputs[key][index] = value
            with self.subTest(key=key,index=index,value=value), self.assertRaises(ValueError):
                calculate_mixing_volumes(**inputs)
        for count in (-1,float('nan'),float('inf')):
            with self.assertRaises(ValueError):
                calculate_mixing_volumes(**{**DEFAULT,'number_of_staples':count})
        with self.assertRaises(ValueError):
            calculate_mixing_volumes(**{**DEFAULT,'desired':[100]})

    def test_rgb_and_manual_use_identical_shared_function(self):
        import image_encoding
        import picklist_app
        self.assertIs(image_encoding.calculate_mixing_volumes, calculate_mixing_volumes)
        self.assertIs(picklist_app.calculate_mixing_volumes, calculate_mixing_volumes)
