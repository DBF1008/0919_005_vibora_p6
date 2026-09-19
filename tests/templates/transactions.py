from vibora.templates import TemplateEngine, Template
from vibora.templates.exceptions import ConflictingNames, InvalidTag
from vibora.tests import TestSuite


class TransactionalLoadingSuite(TestSuite):

    def setUp(self):
        self.engine = TemplateEngine()

    def test_add_template_with_all_conflicting_names_leaves_no_side_effects(self):
        self.engine.add_template(Template('original'), ['existing'])
        before = dict(self.engine.templates)
        with self.assertRaises(ConflictingNames):
            self.engine.add_template(Template('intruder'), ['existing'])
        self.assertEqual(before, self.engine.templates)

    def test_add_template_with_invalid_syntax_leaves_no_side_effects(self):
        before = dict(self.engine.templates)
        with self.assertRaises(InvalidTag):
            self.engine.add_template(Template('{% invalidtag %}'), ['broken'])
        self.assertEqual(before, self.engine.templates)

    def test_batch_load_is_all_or_nothing_on_conflict(self):
        # Simulates the 50-templates scenario: the 49th template conflicts
        # and none of the previous 48 registrations may survive.
        self.engine.add_template(Template('pre-existing'), ['template_48'])
        before = dict(self.engine.templates)
        batch = [(Template(f'content {index}'), [f'template_{index}']) for index in range(50)]
        with self.assertRaises(ConflictingNames):
            self.engine.add_templates(batch)
        self.assertEqual(before, self.engine.templates)
        self.assertEqual(len(self.engine.templates), 1)

    def test_batch_load_is_all_or_nothing_on_invalid_template(self):
        batch = [
            (Template('valid one'), ['valid_one']),
            (Template('{% invalidtag %}'), ['broken']),
        ]
        with self.assertRaises(InvalidTag):
            self.engine.add_templates(batch)
        self.assertNotIn('valid_one', self.engine.templates)
        self.assertEqual(len(self.engine.templates), 0)

    def test_batch_load_success_registers_everything(self):
        batch = [(Template(f'content {index}'), [f'template_{index}']) for index in range(50)]
        parsed = self.engine.add_templates(batch)
        self.assertEqual(len(parsed), 50)
        self.assertEqual(len(self.engine.templates), 50)
        for index in range(50):
            self.assertIn(f'template_{index}', self.engine.templates)

    def test_batch_load_detects_conflicts_inside_the_batch_itself(self):
        batch = [
            (Template('first'), ['shared_name']),
            (Template('second'), ['shared_name']),
        ]
        with self.assertRaises(ConflictingNames):
            self.engine.add_templates(batch)
        self.assertEqual(len(self.engine.templates), 0)

    def test_batch_load_keeps_non_conflicting_aliases(self):
        self.engine.add_template(Template('old'), ['index.html'])
        parsed = self.engine.add_templates([(Template('new'), ['index.html', 'pages/index.html'])])
        self.assertEqual(self.engine.get_template('index.html').content, 'old')
        self.assertEqual(self.engine.get_template('pages/index.html').content, 'new')
        self.assertEqual(len(parsed), 1)

    def test_transaction_context_manager_rolls_back_on_error(self):
        self.engine.add_template(Template('stable'), ['stable'])
        try:
            with self.engine.transaction():
                self.engine.templates['unstable'] = object()
                raise RuntimeError('boom')
        except RuntimeError:
            pass
        self.assertNotIn('unstable', self.engine.templates)
        self.assertIn('stable', self.engine.templates)
