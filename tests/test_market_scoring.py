import unittest
from market_engine import MarketEngine, should_publish_market, should_publish_pulse

def coin(coin_id, price):
    return {"id": coin_id, "symbol": coin_id, "current_price": price}

class MarketScoringTests(unittest.TestCase):
    def test_broad_rally_regime_and_score(self):
        e=MarketEngine('https://example.test')
        old=[coin(x,100) for x in ('bitcoin','ethereum','solana','ripple','cardano')]
        new=[coin(x,108) for x in ('bitcoin','ethereum','solana','ripple','cardano')]
        e.update_history(old,0); e.update_history(new,3600)
        s=e.analyze(new,{},3600)
        self.assertEqual(s.regime,'BROAD_RALLY'); self.assertEqual(s.score,11)
        self.assertEqual(s.breadth['positive_pct'],100.0)
        self.assertTrue(should_publish_market(s)); self.assertTrue(should_publish_pulse(s))

    def test_broad_selloff_regime(self):
        e=MarketEngine('https://example.test')
        old=[coin(x,100) for x in ('bitcoin','ethereum','solana','ripple','cardano')]
        new=[coin(x,90) for x in ('bitcoin','ethereum','solana','ripple','cardano')]
        e.update_history(old,0); e.update_history(new,3600)
        s=e.analyze(new,{},3600)
        self.assertEqual(s.regime,'BROAD_SELLOFF'); self.assertEqual(s.score,11)
        self.assertEqual(s.breadth['negative_pct'],100.0)
        self.assertTrue(should_publish_market(s)); self.assertTrue(should_publish_pulse(s))

    def test_btc_led_move(self):
        e=MarketEngine('https://example.test')
        old=[coin(x,100) for x in ('bitcoin','ethereum','solana','ripple')]
        new=[coin('bitcoin',102),coin('ethereum',100),coin('solana',100),coin('ripple',100)]
        e.update_history(old,0); e.update_history(new,3600)
        s=e.analyze(new,{},3600)
        self.assertEqual(s.regime,'BTC_LED_MOVE'); self.assertEqual(s.score,4)
        self.assertFalse(should_publish_market(s)); self.assertTrue(should_publish_pulse(s))

    def test_market_divergence(self):
        e=MarketEngine('https://example.test')
        old=[coin(x,100) for x in ('bitcoin','ethereum','solana','ripple')]
        new=[coin('bitcoin',101),coin('ethereum',99),coin('solana',100),coin('ripple',100)]
        e.update_history(old,0); e.update_history(new,3600)
        s=e.analyze(new,{},3600)
        self.assertEqual(s.regime,'MARKET_DIVERGENCE'); self.assertTrue(should_publish_pulse(s))

    def test_trigger_threshold_and_sorting(self):
        e=MarketEngine('https://example.test')
        old=[coin(x,100) for x in ('bitcoin','ethereum','solana')]
        new=[coin('bitcoin',104),coin('ethereum',97),coin('solana',101)]
        e.update_history(old,0); e.update_history(new,3600)
        s=e.analyze(new,{},3600)
        self.assertEqual([t['id'] for t in s.triggers],['bitcoin','ethereum'])

if __name__=='__main__': unittest.main()
