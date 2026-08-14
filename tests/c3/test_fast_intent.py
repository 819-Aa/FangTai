"""FastIntentRouter 单元测试 —— 确定性意图路由（P3）。"""

from food_agent_v2.c3.fast_intent import FastIntentRouter


class TestDishCount:
    def test_three_dishes_one_soup(self):
        assert FastIntentRouter.route("想做个三菜一汤").dish_count_requested == 4

    def test_four_dishes_one_soup(self):
        assert FastIntentRouter.route("四菜一汤，营养均衡").dish_count_requested == 5

    def test_two_dishes_one_soup(self):
        assert FastIntentRouter.route("两菜一汤").dish_count_requested == 3

    def test_no_dish_count(self):
        assert FastIntentRouter.route("今晚吃啥比较好").dish_count_requested is None


class TestTimeConstraint:
    def test_half_hour_hard(self):
        d = FastIntentRouter.route("半小时内能搞定的晚饭")
        assert d.time_constraint_seconds == 1800
        assert d.time_constraint_policy == "hard"

    def test_n_minutes_hard(self):
        d = FastIntentRouter.route("30分钟内完成")
        assert d.time_constraint_seconds == 1800
        assert d.time_constraint_policy == "hard"

    def test_soft_fast(self):
        d = FastIntentRouter.route("尽量快一点")
        assert d.time_constraint_policy == "flexible"


class TestTabooExclusions:
    def test_no_spicy(self):
        d = FastIntentRouter.route("别做辣的")
        assert d.health_exclusions == ("p1:禁忌:辣椒",)

    def test_not_too_sweet_is_soft_preference(self):
        # "别太甜" 是口味偏好，不是健康禁忌
        d = FastIntentRouter.route("别太甜")
        assert d.health_exclusions == ()
        assert "甜" in d.preference_exclusions

    def test_light_flavor_not_taboo(self):
        # "清淡" 是口味偏好，不是禁忌
        d = FastIntentRouter.route("口味清淡一点")
        assert d.health_exclusions == ()
        assert "清淡" in d.flavor_preferences


class TestIntent:
    def test_new_recommendation_default(self):
        assert FastIntentRouter.route("今晚吃啥").intent == "new_recommendation"

    def test_replace(self):
        assert FastIntentRouter.route("把红烧肉换成清蒸鱼").intent == "replace"

    def test_reject(self):
        assert FastIntentRouter.route("重新推荐一批").intent == "reject_plan"
