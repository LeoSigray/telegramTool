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
def timeseries(days: int = 14, niche: str | None = None) -> list[dict]:
    return an.timeseries(days=days, niche=niche)


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


class AccountCostIn(BaseModel):
    cost: float = Field(ge=0, description="Сколько стоил аккаунт")
    source: str | None = Field(default=None, description="own | lzt | tdata")


@router.post("/accounts/{name}/cost")
def set_cost(name: str, body: AccountCostIn) -> dict:
    """Без цены аккаунтов CPL посчитать нельзя — проставьте её один раз."""
    an.set_account_cost(name, body.cost, body.source)
    return {"ok": True, "account": an.get_account(name)}


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
    from . import gemini
    if not gemini.is_configured():
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY не задан в .env")

    try:
        variants = await gemini.generate_dm_variants(body.niche, body.info, body.count)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Gemini: {e}")

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
    send_id: int
    outcome: str = Field(pattern="^(lead|negative|replied|no_reply)$")


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
