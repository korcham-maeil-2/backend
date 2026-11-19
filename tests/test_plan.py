"""1년 정착 플랜 (이슈 #18): 계산 결과·월세 시세·청년 지원에서 달마다 할 일을 만든다."""

from datetime import date

from conftest import scenario_text
from test_demo_scenarios import profile_a
from test_support import NAJU, FakeSource, api, naju_profile, pol, scenario_table

from backend.calc.summary import calculate_all
from backend.plan import ANYTIME_MAX, build_plan

TODAY = date(2026, 11, 15)
NAJU_RENT = {
    NAJU: {
        'lawd_cd': NAJU,
        'region': '전남광주통합특별시 나주시',
        'monthly_rent_krw': 300_000,
        'deposit_krw': 3_000_000,
        'sample_size': 344,
        'period': '2026년 5~10월',
        'source': '국토부 실거래가',
        'status': 'estimated',
    }
}


def policy(pid: str, name: str, **kw) -> dict:
    """LocalSupport 안의 정책 한 건 (기본: 신청 중인 이사·주거 정책)."""
    return {
        'id': pid,
        'name': name,
        'category': '주거',
        'support': '월 20만원',
        'agency': '나주시',
        'age_range': '19~34세',
        'income': '소득 무관',
        'conditions': None,
        'scope': 'local',
        'fit': 'moving',
        'apply_status': 'open',
        'apply_period': '2026-01-01 ~ 2026-12-31',
        'url': 'https://example.go.kr',
        **kw,
    }


def support(local: list[dict], national: list[dict] = (), posting: str = 'posting-1') -> dict:
    return {
        'regions': [
            {
                'lawd_cd': NAJU,
                'region': '전남광주통합특별시 나주시',
                'posting_ids': [posting],
                'policies': local,
                'matched': len(local),
            }
        ],
        'national': list(national),
        'national_matched': len(national),
        'age': 27,
        'checked_on': TODAY.isoformat(),
    }


def naju(**costs) -> tuple[list[dict], dict]:
    """나주 공고 하나의 비교표와 계산 결과 (서울에서 통근 불가 → 이사만)."""
    table = scenario_table('down_naju')
    profile = naju_profile(27)
    profile['postings']['posting-1'] = {**profile['postings']['posting-1'], **costs}
    return table, calculate_all(table, profile, NAJU_RENT)


def slots_of(plan) -> dict[str, list[str]]:
    [posting] = plan.postings
    return {s.label: [i.title for i in s.items] for s in posting.slots}


def test_moving_plan_puts_each_task_in_its_month():
    table, result = naju()
    local = [
        policy('A', '나주 청년 월세 지원', apply_period='2026-01-01 ~ 2026-12-31'),
        policy(
            'B', '나주 이사비 지원', apply_status='upcoming', apply_period='2027-02-01 ~ 2027-03-31'
        ),
        policy('C', '청년 전세자금 대출', apply_status='always', apply_period='상시'),
        policy('D', '청년 월세 특별지원', apply_status='always', apply_period='상시'),
        policy('E', '청년 주거 상담', apply_status='unknown', apply_period=None),
    ]
    plan = build_plan(result, support(local), table, TODAY)
    assert plan.start_month == '2026-11'
    assert slots_of(plan) == {
        '이사 전': ['보증금 마련', '청년 전세자금 대출'],  # 보증금·전세 대출은 이사 전에
        '이사한 달': ['전입신고', '청년 월세 특별지원'],  # 상시 월세 지원은 전입한 뒤에
        '2026년 12월': ['나주 청년 월세 지원'],  # 신청 중 → 마감 달
        '2027년 2월': ['나주 이사비 지원'],  # 곧 시작 → 시작 달
        '입사 3개월 뒤': ['수습이 끝나요'],  # 공고에서 읽은 날 (수습 중 90%)
        '입사 24개월 뒤': ['회사 주거지원비가 끝나요'],
        '언제든': ['청년 주거 상담'],
    }
    [posting] = plan.postings
    deposit = posting.slots[0].items[0]
    assert deposit.amount_krw == 3_000_000 and deposit.status == 'estimated'
    assert '344건' in deposit.source
    [dec] = [s for s in posting.slots if s.month == '2026-12']
    assert dec.items[0].when == '12월 31일 마감'
    move_in = posting.slots[1].items[0]
    assert move_in.when is None and '14일 안' in move_in.detail and '주민등록법' in move_in.source


def dorm_table() -> list[dict]:
    """나주 공고를 '사택·기숙사 있음, 주거지원비 없음'으로 바꾼 비교표."""
    table, _ = naju()
    row = table[0]
    return [
        {
            **row,
            'company_housing': {**row['company_housing'], 'value': True},
            'housing_allowance_monthly_krw': {
                **row['housing_allowance_monthly_krw'],
                'value': None,
            },
        }
    ]


def test_company_housing_resident_gets_no_deposit_or_housing_support():
    # 사택·기숙사가 있는 공고에 이사할 집 주거비를 0원으로 넣었다 → 사택에 들어간다
    table = dorm_table()
    profile = naju_profile(27)
    profile['postings']['posting-1'] = {
        **profile['postings']['posting-1'],
        'move_housing': {'value': 0, 'status': 'user_input'},
    }
    result = calculate_all(table, profile, NAJU_RENT)
    local = [
        policy('A', '나주 청년 월세 지원', apply_period='2026-01-01 ~ 2026-12-31'),
        policy('C', '청년 전세자금 대출', apply_status='always', apply_period='상시'),
    ]
    plan = build_plan(result, support(local), table, TODAY)
    slots = slots_of(plan)
    assert '보증금 마련' not in sum(slots.values(), [])
    assert '청년 전세자금 대출' not in sum(slots.values(), [])  # 집 구하기·월세 지원도 빠진다
    assert '나주 청년 월세 지원' not in sum(slots.values(), [])
    assert slots['이사한 달'] == ['전입신고']  # 사택 주소로 전입신고는 한다
    [move_in] = [s for s in plan.postings[0].slots if s.kind == 'move_month'][0].items
    assert '회사에 먼저 물어보세요' in move_in.detail


def test_company_housing_but_renting_keeps_the_deposit():
    # 사택이 있어도 주거비를 넣었으면 따로 집을 구하는 것으로 본다
    table = dorm_table()
    profile = naju_profile(27)
    profile['postings']['posting-1'] = {
        **profile['postings']['posting-1'],
        'move_housing': {'value': 400_000, 'status': 'user_input'},
    }
    plan = build_plan(calculate_all(table, profile, NAJU_RENT), support([]), table, TODAY)
    assert '보증금 마련' in slots_of(plan)['이사 전']


def test_rent_cancelled_by_housing_aid_is_not_company_housing():
    # 사택도 있고 주거지원비(나주 공고는 월 20만원)도 있는데 월세 15만원을 넣었다
    # → 이사 주거비는 0원이지만 따로 집을 구한 것이다: 보증금 할 일을 그대로 둔다
    table, _ = naju()
    table = [{**table[0], 'company_housing': {**table[0]['company_housing'], 'value': True}}]
    profile = naju_profile(27)
    profile['postings']['posting-1'] = {
        **profile['postings']['posting-1'],
        'move_housing': {'value': 150_000, 'status': 'user_input'},
    }
    plan = build_plan(calculate_all(table, profile, NAJU_RENT), support([]), table, TODAY)
    assert '보증금 마련' in slots_of(plan)['이사 전']


def test_targeted_and_undated_general_policies_stay_out():
    """특정 대상 정책, 상시 일반 정책(상담·행사 등)은 플랜에 넣지 않는다 (지원 목록에는 있다)."""
    table, result = naju()
    local = [
        policy('T', '자립준비청년 정착금', fit='targeted'),
        policy(
            'G1', '청년 마음건강센터', fit='general', apply_status='always', apply_period='상시'
        ),
        policy('G2', '청년 문화패스', fit='general', apply_period='2026-11-01 ~ 2026-11-30'),
    ]
    assert slots_of(build_plan(result, support(local), table, TODAY)) == {
        '이사 전': ['보증금 마련'],
        '이사한 달': ['전입신고'],
        '2026년 11월': ['청년 문화패스'],
        '입사 3개월 뒤': ['수습이 끝나요'],
        '입사 24개월 뒤': ['회사 주거지원비가 끝나요'],
    }


def test_far_dates_are_left_out_or_marked():
    """12개월 밖에 시작하는 정책은 넣지 않고, 마감만 먼 신청 중 정책은 '언제든'에 마감일과 함께."""
    table, result = naju()
    local = [
        policy(
            'F', '먼 미래 지원', apply_status='upcoming', apply_period='2028-01-01 ~ 2028-02-01'
        ),
        policy('L', '긴 신청 기간', apply_period='2026-01-01 ~ 2028-06-30'),
    ]
    plan = build_plan(result, support(local), table, TODAY)
    assert slots_of(plan)['언제든'] == ['긴 신청 기간']
    assert plan.postings[0].slots[-1].items[0].when == '2028년 6월 30일까지'
    assert '먼 미래 지원' not in str(slots_of(plan))


def test_anytime_is_capped_with_a_note():
    table, result = naju()
    many = [
        policy(f'P{i}', f'주거 정책 {i}', apply_status='always', apply_period='상시')
        for i in range(ANYTIME_MAX + 3)
    ]
    plan = build_plan(result, support(many), table, TODAY)
    assert len(slots_of(plan)['언제든']) == ANYTIME_MAX
    assert any('3개 더' in n for n in plan.postings[0].notes)


def test_commute_only_plan_has_no_move_tasks_and_no_local_policies():
    """통근만 하는 경우: 보증금·전입신고·지역 정책·주거 정책은 빼고 전국 일반 정책만."""
    table = scenario_table('near_pangyo')
    profile = profile_a()
    pangyo = {**profile['postings']['near_pangyo'], 'move_possible': False}
    profile['postings'] = {'posting-1': pangyo}
    result = calculate_all(table, profile, {})
    national = [
        policy('N1', '전국 청년 월세 지원', scope='national'),
        policy('N2', '청년도약계좌', scope='national', fit='general', category='금융･복지･문화'),
    ]
    plan = build_plan(result, support([policy('L', '판교 월세')], national), table, TODAY)
    assert slots_of(plan) == {'2026년 12월': ['청년도약계좌']}
    assert plan.postings[0].moving is False
    assert any('사는 곳을 몰라' in n for n in plan.postings[0].notes)
    assert plan.alternates == []  # 이사가 해당되지 않으면 바꿔 볼 플랜이 없다


def test_without_support_plan_keeps_move_tasks():
    table, result = naju()
    plan = build_plan(result, None, table, TODAY)
    assert slots_of(plan) == {
        '이사 전': ['보증금 마련'],
        '이사한 달': ['전입신고'],
        '입사 3개월 뒤': ['수습이 끝나요'],
        '입사 24개월 뒤': ['회사 주거지원비가 끝나요'],
    }
    assert any('청년 지원 정보를 불러오지 못해' in n for n in plan.notes)


def test_no_result_no_plan():
    assert build_plan(None, None, [], TODAY) is None


def test_jeonwolse_guarantee_goes_before_moving():
    """'청년 전월세 대출보증'은 보증금 대출 보증이라 '이사 전'.
    '월세'로 잡혀 이사한 달에 가면 안 된다 (실제 온통청년 데이터로 확인하다 찾은 경우)."""
    table, result = naju()
    local = [policy('J', '청년 전월세 대출보증', apply_status='always', apply_period='상시')]
    assert slots_of(build_plan(result, support(local), table, TODAY))['이사 전'] == [
        '보증금 마련',
        '청년 전월세 대출보증',
    ]


def test_jamo_bullets_are_removed():
    table, result = naju()
    raw = 'ㅁ대상: 만 19~34세 무주택 청년\nㅇ (전세) 보증금 5억원 이하'
    local = [policy('S', '청년 월세 대출', support=raw)]
    item = build_plan(result, support(local), table, TODAY).postings[0].slots[2].items[0]
    assert item.detail == '대상: 만 19~34세 무주택 청년 (전세) 보증금 5억원 이하'


def test_policy_summary_is_one_clean_line():
    table, result = naju()
    raw = '□ 대출금리\n(우대형) 연 1.3%\n※ 지방은 0.2%p 인하\n - 한도 1억원'
    local = [policy('S', '청년 월세 대출', support=raw)]
    item = build_plan(result, support(local), table, TODAY).postings[0].slots[2].items[0]
    assert item.detail == '대출금리 (우대형) 연 1.3% 지방은 0.2%p 인하 한도 1억원'


def test_session_response_includes_plan(tmp_path):
    """계산을 마친 세션 응답에 정착 플랜이 들어 있다 (확인 전에는 없다)."""
    source = FakeSource({NAJU: [pol(plcyNo='A', plcyNm='나주 청년 월세 지원')]})
    with api(tmp_path, source) as c:
        sid = c.post('/sessions', json={'message': scenario_text('down_naju')}).json()['session_id']
        assert c.get(f'/sessions/{sid}').json()['plan'] is None
        done = c.post(
            f'/sessions/{sid}/confirm', json={'approved': True, 'profile': naju_profile(27)}
        ).json()
        [posting] = done['plan']['postings']
        assert posting['moving'] is True
        titles = [i['title'] for s in posting['slots'] for i in s['items']]
        assert '전입신고' in titles and '나주 청년 월세 지원' in titles


def test_start_slot_collects_first_job_policies_for_both_cases():
    """입사하면 챙겨 볼 것: 전국 상시 정책 중 첫 취업이면 누구나 해당하는 것 (구직자용은 뺀다)."""
    table, result = naju()
    national = [
        policy(
            'S1',
            '청년 생애 첫 국민연금 보험료 지원',
            scope='national',
            fit='general',
            apply_status='always',
            apply_period=None,
        ),
        policy(
            'S2', '청년주택드림청약통장', scope='national', apply_status='always', apply_period=None
        ),
        policy(
            'X1',
            '청년 구직 상담',
            scope='national',
            fit='general',
            apply_status='always',
            apply_period=None,
        ),
        policy('X2', '지역 학자금 지원', apply_status='always', apply_period=None),  # 지역 정책
    ]
    plan = build_plan(result, support([], national), table, TODAY)
    assert slots_of(plan)['입사하면 챙겨 볼 것'] == [
        '청년 생애 첫 국민연금 보험료 지원',
        '청년주택드림청약통장',
    ]


def test_same_month_policies_sorted_by_date_and_long_first_job_in_start():
    """같은 달 칸 안에서도 날짜순. 마감이 1년 넘게 남은 첫 취업 지원은 '입사하면'에."""
    table, result = naju()
    local = [policy('L', '나주 청년 월세 지원', apply_period='2026-01-01 ~ 2026-12-28')]
    national = [
        policy('N', '청년 이사비 지원', scope='national', apply_period='2026-01-01 ~ 2026-12-03'),
        policy(
            'F',
            '청년주택드림청약통장',
            scope='national',
            fit='general',
            apply_period='2024-02-21 ~ 2099-12-31',
        ),
    ]
    slots = slots_of(build_plan(result, support(local, national), table, TODAY))
    assert slots['2026년 12월'] == ['청년 이사비 지원', '나주 청년 월세 지원']
    assert slots['입사하면 챙겨 볼 것'] == ['청년주택드림청약통장']


def test_commute_alternate_plan_when_both_cases_apply():
    """이사가 해당되면 이사 플랜(postings)과 함께 통근 플랜(alternates)도 만든다.
    통근 플랜에는 보증금·전입신고·지역 정책·날짜 있는 주거 정책이 없다."""
    table = scenario_table('near_pangyo')
    profile = profile_a()
    profile['postings'] = {
        'posting-1': {**profile['postings']['near_pangyo'], 'move_possible': True}
    }
    result = calculate_all(table, profile, {})
    national = [
        policy('N1', '전국 청년 월세 지원', scope='national'),
        policy('N2', '청년도약계좌', scope='national', fit='general', category='금융･복지･문화'),
    ]
    plan = build_plan(result, support([policy('L', '판교 월세')], national), table, TODAY)
    [moving] = plan.postings
    [commute] = plan.alternates
    assert moving.moving is True and commute.moving is False
    assert commute.posting_id == moving.posting_id
    titles = [i.title for s in commute.slots for i in s.items]
    assert titles == ['청년도약계좌']


def test_milestones_from_posting_only_when_money_changes():
    """공고에서 읽은 날: 수습 중 급여가 100% 미만이면 수습 끝,
    이사하면서 주거지원비 기간을 알면 지원 끝."""
    table, result = naju()
    plan = build_plan(result, None, table, TODAY)
    [probation] = next(s for s in plan.postings[0].slots if s.label == '입사 3개월 뒤').items
    assert probation.kind == 'milestone' and '90%' in probation.detail
    [aid] = next(s for s in plan.postings[0].slots if s.label == '입사 24개월 뒤').items
    assert aid.amount_krw == table[0]['housing_allowance_monthly_krw']['value']
    assert all(s.month is None for s in plan.postings[0].slots if s.kind == 'job')
    # 수습 중 100%면 돈이 바뀌지 않아 넣지 않는다
    table[0]['probation_pay_rate']['value'] = 1.0
    labels = slots_of(build_plan(result, None, table, TODAY))
    assert '입사 3개월 뒤' not in labels and '입사 24개월 뒤' in labels


def test_milestones_show_remaining_before_and_after():
    """입사한 뒤 칸: 그날 앞뒤로 매달 남는 돈. 계산 결과(수습 끝·지원 받는 때)에서 거꾸로 짚는다."""

    def paid(**costs):  # 나주 공고는 급여가 내규라, 연봉을 넣었다고 본다
        table = scenario_table('down_naju')
        table[0]['salary_type']['value'] = 'annual'
        table[0]['annual_salary_krw'] = {'value': 36_000_000, 'status': 'user_input'}
        profile = naju_profile(27)
        profile['postings']['posting-1'] = {**profile['postings']['posting-1'], **costs}
        return table, calculate_all(table, profile, NAJU_RENT)

    table, result = paid()
    [posting] = result['postings']
    move = next(s for s in posting['scenarios'] if s['case'] == 'move')
    remaining, net = move['monthly_remaining'], posting['net_income']['value']
    cut = posting['probation_net']
    assert cut is not None and 0 < cut < net  # 수습 90% → 세후 월급도 줄어든다
    aid_krw = table[0]['housing_allowance_monthly_krw']['value']
    plan = build_plan(result, None, table, TODAY)
    item = {s.label: s.items[0] for s in plan.postings[0].slots if s.kind == 'job'}
    probation, aid = item['입사 3개월 뒤'], item['입사 24개월 뒤']
    assert (probation.remaining_before, probation.remaining_after) == (
        remaining - (net - cut),
        remaining,
    )
    assert (aid.remaining_before, aid.remaining_after) == (remaining, remaining - aid_krw)
    # 새 집 주거비 < 지원비면 주거비가 0원으로 막혀 줄어드는 만큼을 모른다 → 금액 없이
    table, result = paid(move_housing={'value': aid_krw // 2})
    plan = build_plan(result, None, table, TODAY)
    assert all(
        i.remaining_before is None and i.remaining_after is None
        for s in plan.postings[0].slots
        if s.kind == 'job'
        for i in s.items
    )
