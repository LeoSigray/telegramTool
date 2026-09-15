"""
api/routes_analytics.py — данные для графиков (ваша CRM) и решения оптимизатора.

Все endpoints под Bearer-токеном, кроме /dashboard (статическая страница,
токен вводится в самой странице и хранится в localStorage браузера).
"""
import os

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from data import analytics as an
from optimizer import bandit, economics, health, planner
from .auth import require_token

router = APIRouter(prefix="/analytics", tags=["analytics"],
                   dependencies=[Depends(require_token)])

page_router = APIRouter(tags=["dashboard"])  # без авторизации — только HTML


# ──────────────────────────────────────────────────────────────────────────
#  Сводка
# ──────────────────────────────────────────────────────────────────────────

@router.get("/overview")
def overview(days: float = 30, niche: str | None = None) -> dict:
    """Всё, что нужно для шапки дашборда, одним запросом."""
    an.expire_pending()
    return {
        "totals": an.totals(),
        "funnel": an.funnel(days=days, niche=niche),
        "cpl": economics.cpl(days=days, niche=niche),
        "capacity": planner.capacity(),
        "cost_per_message": economics.cost_per_message(),
        "niches": an.list_niches(),
    }


@router.get("/funnel")
def funnel(days: float = 30, niche: str | None = None, job_id: str | None = None) -> dict:
    an.expire_pending()
    return an.funnel(days=days, niche=niche, job_id=job_id)


@router.get("/timeseries")
def timeseries(days: float = 14, niche: str | None = None,
               granularity: str = Query(default="day", pattern="^(minute|hour|day)$")) -> list[dict]:
    """granularity — цена деления оси времени. 'minute' имеет смысл только
    для короткого окна (days — доля дня); за 30 дней минутные бакеты дали бы
    десятки тысяч точек. Фронтенд сам подбирает granularity под окно (см.
    granularityFor() в desktop/telegramtool-dashboard/src/index.html)."""
    return an.timeseries(days=days, niche=niche, granularity=granularity)


# ──────────────────────────────────────────────────────────────────────────
#  Аккаунты
# ──────────────────────────────────────────────────────────────────────────

@router.get("/accounts")
def accounts() -> dict:
    return {
        "accounts": health.report(),
        "stats_7d": an.account_stats(days=7),
        "capacity": planner.capacity(),
        "config": {
            "base_daily": health.BASE_DAILY_DM,
            "hard_cap": health.HARD_CAP,
            "error_threshold": health.ERROR_RATE_THRESHOLD,
        },
    }


@router.get("/accounts/events")
def events(account: str | None = None, days: float = 14) -> list[dict]:
    return an.account_events(account=account, days=days)


@router.get("/accounts/acquisition")
def accounts_acquisition(bucket: str = Query(default="week", pattern="^(day|week|month)$"),
                         days: float | None = None) -> dict:
    """График закупки аккаунтов: покупки и выбытие во времени, накопительно
    куплено/живо, траты. days не задан → всё время."""
    return {
        "bucket": bucket,
        "series": an.acquisition_series(bucket=bucket, days=days),
        "totals": an.totals(),
    }


@router.get("/accounts/cohorts")
def accounts_cohorts(by: str = Query(default="price", pattern="^(price|seller)$"),
                     days: float = 90) -> dict:
    """Эффективность аккаунтов в разрезе цены или продавца: пробег, дни жизни,
    % банов, reply-rate, выхлоп за рубль. days — окно для метрик рассылки."""
    return {
        "by": by,
        "cohorts": an.account_cohorts(by=by, days=days),
        "window_days": days,
        "price_brackets": [list(b) for b in an.PRICE_BRACKETS],
    }


# ──────────────────────────────────────────────────────────────────────────
#  Подписки на канал
# ──────────────────────────────────────────────────────────────────────────

class ChannelSyncIn(BaseModel):
    channel: str = Field(min_length=1, description="@username или t.me/ссылка канала")


@router.post("/channel/sync")
async def sync_channel(body: ChannelSyncIn) -> dict:
    """
    Сверяет участников канала со списком, кому писали, проставляет
    subscribed_at новым совпадениям. Нужен хотя бы один аккаунт-админ канала
    в пуле — иначе Telegram отдаёт неполный список участников.
    """
    from . import channel_watch
    try:
        return await channel_watch.sync_channel(body.channel)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/channel/sync-all")
async def sync_all_channels(days: float = 30) -> list[dict]:
    """Синкает все каналы, встречавшиеся в рассылках за последние days дней."""
    from . import channel_watch
    return await channel_watch.sync_all_recent(days=days)


@router.get("/subscriptions/timeseries")
def subscriptions_timeseries(
        bucket_minutes: int = Query(default=10, ge=1, le=60),
        hours: float = Query(default=24, gt=0, le=720),
        channel: str | None = None) -> dict:
    """Темп подписок мелкими бакетами (деф. 10 мин) по реальному времени вступления.
    Для дашборда «Подписки»."""
    an.expire_pending()
    out = an.subscription_series(bucket_minutes=bucket_minutes, hours=hours, channel=channel)
    out["channels"] = an.subscription_channels(days=max(30, hours / 24))
    return out


@router.get("/replies")
def replies(niche: str | None = None, days: float = 30,
            bucket: str = Query(default="day", pattern="^(day|week)$"),
            recent_limit: int = Query(default=40, ge=1, le=200)) -> dict:
    """Дашборд «Ответы на сообщения»: тренд исхода по вариантам (успех/отказ/блок),
    сводная таблица и лента последних ответов."""
    an.expire_pending()
    out = {
        "trend": an.reply_series(niche=niche, days=days, bucket=bucket),
        "by_variant": an.reply_breakdown(niche=niche, days=max(days, 90)),
        "recent": an.recent_replies(niche=niche, limit=recent_limit),
        "niches": an.reply_niches(),
        "min_samples": bandit.MIN_SAMPLES,
    }
    if niche:
        out["win_probability"] = bandit.win_probability(niche)
    return out


@router.post("/replies/reclassify")
def replies_reclassify() -> dict:
    """Прогнать обновлённые списки фраз отказа (HARD_OPT_OUT / SOFT_REJECT в
    data/analytics.py) по уже сохранённым текстам ответов. Пополняет стоп-лист."""
    return an.reclassify_replies()


@router.get("/spend")
def spend(days: float | None = None,
          bucket: str = Query(default="week", pattern="^(day|week|month)$")) -> dict:
    """Дашборд «Потраченные средства»: траты во времени + running-эффективность денег."""
    an.expire_pending()
    return {
        **an.spend_series(days=days, bucket=bucket),
        "by_seller": an.spend_by_seller(days=days),
        "cpl_modeled": economics.cpl(days=days or 30).get("cpl_modeled"),
        "cost_per_message": economics.cost_per_message(),
    }


class AccountCostIn(BaseModel):
    cost: float = Field(ge=0, description="Сколько стоил аккаунт")
    source: str | None = Field(default=None, description="own | lzt | tdata | session")
    seller: str | None = Field(default=None, description="Продавец (для разреза эффективности)")
    acquired_at: str | None = Field(default=None, description="ISO-дата покупки")


@router.post("/accounts/{name}/cost")
def set_cost(name: str, body: AccountCostIn) -> dict:
    """Без цены аккаунтов CPL посчитать нельзя — проставьте её один раз."""
    an.set_account_cost(name, body.cost, body.source, body.seller, body.acquired_at)
    return {"ok": True, "account": an.get_account(name)}


class AccountCostRow(AccountCostIn):
    name: str = Field(min_length=1)


@router.post("/accounts/costs")
def set_costs_bulk(rows: list[AccountCostRow]) -> dict:
    """Массово проставить цену/продавца/дату покупки — например, залить из своей
    таблицы за один запрос."""
    updated = []
    for r in rows:
        an.set_account_cost(r.name, r.cost, r.source, r.seller, r.acquired_at)
        updated.append(r.name)
    return {"ok": True, "updated": updated, "count": len(updated)}


@router.post("/accounts/backfill")
def backfill_accounts(force: bool = False) -> dict:
    """Восстановить source/cost/seller/acquired_at для аккаунтов, заведённых до
    появления учёта покупки: из accounts/*.txt (LZT) и mtime .session-файлов."""
    from accounts.backfill import backfill_account_meta
    return backfill_account_meta(force=force)


@router.post("/accounts/sweep")
def sweep() -> dict:
    """Пересчёт множителей и снятие отдыха. Вызывается сервером раз в час."""
    return health.sweep()


# ──────────────────────────────────────────────────────────────────────────
#  Шаблоны и A/B
# ──────────────────────────────────────────────────────────────────────────

class TemplateIn(BaseModel):
    niche: str = Field(min_length=1)
    text: str = Field(min_length=1)
    variant: str | None = None


@router.post("/templates")
def create_template(body: TemplateIn) -> dict:
    tid = an.add_template(body.niche, body.text, body.variant)
    return {"ok": True, "template": an.get_template(tid)}


class GenerateTemplatesIn(BaseModel):
    niche: str = Field(min_length=1, description="Ниша/индустрия клиента")
    info: str = Field(default="", description="Доп. контекст: оффер, тон, что за бизнес")
    count: int = Field(default=2, ge=1, le=5, description="Сколько РАЗНЫХ вариантов сгенерировать")


@router.post("/templates/generate")
async def generate_templates(body: GenerateTemplatesIn) -> dict:
    """
    Ниша + инфо → нейронка пишет N разных вариантов первого сообщения →
    сразу сохраняются как шаблоны этой ниши, готовые для бандита.

    Если для ниши уже есть активные шаблоны — новые добавятся вариантами
    C/D/... и бандит начнёт сравнивать их наравне со старыми (со скидкой
    на то, что у старых уже есть история, а у новых её пока нет — см.
    MIN_SAMPLES в optimizer/bandit.py).
    """
    from . import copywriter
    if not copywriter.is_configured():
        raise HTTPException(status_code=503, detail=copywriter.config_hint())

    try:
        variants = await copywriter.generate_dm_variants(body.niche, body.info, body.count)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"llm: {e}")

    created = [an.get_template(an.add_template(body.niche, text)) for text in variants]
    return {"ok": True, "niche": body.niche, "templates": created}


@router.get("/templates")
def list_templates(niche: str | None = None) -> dict:
    an.expire_pending()
    out = {"templates": bandit.posterior(niche)}
    if niche:
        out["win_probability"] = bandit.win_probability(niche)
        out["prune_suggestions"] = bandit.prune_suggestions(niche)
        out["min_samples"] = bandit.MIN_SAMPLES
    return out


@router.get("/copy/analyze")
async def copy_analyze(niche: str) -> dict:
    """
    AI-редактор текстов: Claude смотрит статистику бандита + реальные ответы
    людей + отказы по нише и выдаёт разбор, новые варианты и «шкалу тона».
    Ничего не сохраняет — новые варианты добавляются через /copy/apply.
    """
    from . import copywriter
    from optimizer import copy_optimizer
    if not copywriter.is_configured():
        raise HTTPException(status_code=503, detail=copywriter.config_hint())
    try:
        return await copy_optimizer.analyze_niche(niche)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


class CopyApplyIn(BaseModel):
    niche: str = Field(min_length=1)
    texts: list[str] = Field(min_length=1, description="Тексты новых вариантов")


@router.post("/copy/apply")
def copy_apply(body: CopyApplyIn) -> dict:
    """Сохранить выбранные варианты AI-редактора как шаблоны ниши → в бандит."""
    created = []
    for text in body.texts:
        t = text.strip()
        if t:
            created.append(an.get_template(an.add_template(body.niche, t)))
    return {"ok": True, "niche": body.niche, "templates": created,
            "count": len(created)}


@router.patch("/templates/{template_id}")
def toggle_template(template_id: int, active: bool) -> dict:
    if an.get_template(template_id) is None:
        raise HTTPException(status_code=404, detail="template not found")
    an.set_template_active(template_id, active)
    return {"ok": True, "template": an.get_template(template_id)}


# ──────────────────────────────────────────────────────────────────────────
#  Экономика
# ──────────────────────────────────────────────────────────────────────────

@router.get("/cpl")
def cost_per_lead(days: float = 30, niche: str | None = None) -> dict:
    an.expire_pending()
    return economics.cpl(days=days, niche=niche)


@router.get("/cpl/by-niche")
def cpl_by_niche(days: float = 30) -> list[dict]:
    an.expire_pending()
    return economics.cpl_by_niche(days=days)


@router.get("/buy-decision")
def buy_decision(value_per_lead: float = Query(..., gt=0),
                 price: float | None = None,
                 niche: str | None = None) -> dict:
    """Стоит ли докупать аккаунт при такой ценности лида."""
    return economics.should_buy_account(value_per_lead, price=price, niche=niche)


@router.get("/forecast")
def forecast(targets_remaining: int = Query(..., ge=0), niche: str | None = None) -> dict:
    return economics.forecast(targets_remaining, niche=niche)


# ──────────────────────────────────────────────────────────────────────────
#  Исходы из CRM
# ──────────────────────────────────────────────────────────────────────────

class OutcomeIn(BaseModel):
    # новые имена + старые (replied/negative) для обратной совместимости с CRM
    send_id: int
    outcome: str = Field(pattern="^(lead|success|rejected|blocked|no_reply|replied|negative)$")


@router.post("/outcome")
def set_outcome(body: OutcomeIn) -> dict:
    """
    CRM сообщает финальный исход. Без этого «лид» системе неизвестен
    и CPL остаётся модельным, а не фактическим.
    """
    if not an.mark_outcome(body.send_id, body.outcome):
        raise HTTPException(status_code=404, detail="send not found")
    return {"ok": True}


class SuppressIn(BaseModel):
    key: str = Field(min_length=1, description="@username или user_id")
    reason: str = "manual"


@router.post("/suppress")
def suppress(body: SuppressIn) -> dict:
    """Стоп-лист: этому человеку больше никогда не пишем."""
    an.suppress(body.key, body.reason)
    return {"ok": True}


@router.get("/suppression")
def suppression() -> list[dict]:
    return an.suppression_list()


# ──────────────────────────────────────────────────────────────────────────
#  План (dry-run перед запуском)
# ──────────────────────────────────────────────────────────────────────────

class PlanIn(BaseModel):
    targets: list[str] = Field(min_length=1)
    niche: str | None = None
    value_per_lead: float | None = None


@router.post("/plan")
def plan(body: PlanIn) -> dict:
    """
    Что будет, если запустить рассылку прямо сейчас: сколько уйдёт сегодня,
    сколько отсеется, каким текстом, во сколько обойдётся. Ничего не отправляет.
    """
    an.expire_pending()
    return planner.build_plan(body.targets, niche=body.niche,
                              value_per_lead=body.value_per_lead)


# ──────────────────────────────────────────────────────────────────────────
#  Дашборд
# ──────────────────────────────────────────────────────────────────────────

@page_router.get("/dashboard", include_in_schema=False)
def dashboard() -> FileResponse:
    path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    return FileResponse(path, media_type="text/html")
