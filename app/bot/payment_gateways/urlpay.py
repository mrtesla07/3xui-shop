import hashlib
import logging
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import aiohttp
from aiogram import Bot
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.utils.i18n import I18n
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n import lazy_gettext as __
from aiohttp.web import Application, Request, Response
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.models import ServicesContainer, SubscriptionData
from app.bot.payment_gateways import PaymentGateway
from app.bot.utils.constants import URLPAY_WEBHOOK, Currency, TransactionStatus
from app.bot.utils.formatting import format_device_count, format_subscription_period
from app.bot.utils.navigation import NavSubscription
from app.config import Config
from app.db.models import Transaction

logger = logging.getLogger(__name__)


class UrlPay(PaymentGateway):
    name = ""
    currency = Currency.RUB
    callback = NavSubscription.PAY_URLPAY
    _base_url = "https://urlpay.io/api"

    def __init__(
        self,
        app: Application,
        config: Config,
        session: async_sessionmaker,
        storage: RedisStorage,
        bot: Bot,
        i18n: I18n,
        services: ServicesContainer,
    ) -> None:
        self.name = _("payment:gateway:urlpay")
        self.app = app
        self.config = config
        self.session = session
        self.storage = storage
        self.bot = bot
        self.i18n = i18n
        self.services = services

        self.app.router.add_post(URLPAY_WEBHOOK, self.webhook_handler)
        logger.info("UrlPay payment gateway initialized.")

    async def create_payment(self, data: SubscriptionData) -> str:
        if not all(
            [
                self.config.urlpay.API_KEY,
                self.config.urlpay.SHOP_ID,
                self.config.urlpay.SECRET_KEY,
            ]
        ):
            raise RuntimeError("UrlPay credentials are not configured.")

        bot_username = (await self.bot.get_me()).username
        redirect_url = f"https://t.me/{bot_username}"

        description = _("payment:invoice:description").format(
            devices=format_device_count(data.devices),
            duration=format_subscription_period(data.duration),
        )

        amount = (
            Decimal(str(data.price))
            .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )
        amount_str = format(amount, ".2f")

        order_uuid = str(uuid.uuid4())

        payload: dict[str, Any] = {
            "currency": self.currency.code.lower(),
            "amount": amount_str,
            "uuid": order_uuid,
            "shopId": self.config.urlpay.SHOP_ID,
            "description": description,
            "website_url": redirect_url,
            "language": "ru",
            "sign": self._generate_signature(amount_str),
            "items": [
                {
                    "description": description,
                    "quantity": 1,
                    "price": amount_str,
                    "vat_code": 0,
                    "payment_subject": 4,
                    "payment_mode": 1,
                }
            ],
        }

        headers = {
            "Authorization": f"Bearer {self.config.urlpay.API_KEY}",
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            url = f"{self._base_url}/v2/payments"
            async with session.post(url, json=payload, headers=headers) as response:
                result = await response.json()
                if response.status != 201 or not result.get("success"):
                    raise RuntimeError(
                        f"UrlPay create payment failed: status={response.status}, body={result}"
                    )

        payment_id = str(result["id"])
        payment_url = result.get("paymentUrl")
        if not payment_url:
            raise RuntimeError("UrlPay response does not contain payment URL.")

        async with self.session() as session:
            await Transaction.create(
                session=session,
                tg_id=data.user_id,
                subscription=data.pack(),
                payment_id=payment_id,
                payment_uuid=order_uuid,
                status=TransactionStatus.PENDING,
            )

        logger.info(f"Payment link created for user {data.user_id}: {payment_url}")
        return payment_url

    async def handle_payment_succeeded(self, payment_id: str) -> None:
        await self._on_payment_succeeded(payment_id)

    async def handle_payment_canceled(self, payment_id: str) -> None:
        await self._on_payment_canceled(payment_id)

    async def webhook_handler(self, request: Request) -> Response:
        try:
            payload = await request.json()
            logger.debug(f"Received UrlPay webhook payload: {payload}")

            payment_status_raw = payload.get("payment_status", "")
            payment_status = str(payment_status_raw).lower()
            if payment_status not in {"success", "cancel"}:
                logger.warning(f"Unsupported UrlPay status: {payment_status_raw}")
                return Response(status=400)

            payment_id = await self._verify_callback(payload, payment_status)
            if not payment_id:
                logger.warning("UrlPay webhook verification failed.")
                return Response(status=400)

            if payment_status == "success":
                await self.handle_payment_succeeded(payment_id)
            else:
                await self.handle_payment_canceled(payment_id)

            return Response(status=200)

        except Exception as exception:
            logger.exception(f"Error processing UrlPay webhook: {exception}")
            return Response(status=400)

    def _generate_signature(self, amount: str) -> str:
        payload = (
            f"{self.currency.code.lower()}"
            f"{amount}"
            f"{self.config.urlpay.SHOP_ID}"
            f"{self.config.urlpay.SECRET_KEY}"
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    async def _verify_callback(self, payload: dict[str, Any], payment_status: str) -> str | None:
        payment_id_raw = payload.get("id")
        if payment_id_raw is None:
            logger.warning("UrlPay callback payload does not contain payment id.")
            return None

        payment_id = str(payment_id_raw)
        payment_uuid = payload.get("uuid")
        if not payment_uuid:
            logger.warning(
                "UrlPay callback payload does not contain uuid for payment %s.",
                payment_id,
            )
            return None

        async with self.session() as session:
            transaction = await Transaction.get_by_id(session=session, payment_id=payment_id)

        if not transaction:
            logger.warning(
                "UrlPay callback received for unknown payment id %s.",
                payment_id,
            )
            return None

        if transaction.payment_uuid:
            if str(transaction.payment_uuid) != str(payment_uuid):
                logger.warning(
                    "UrlPay callback uuid mismatch with database: payload=%s, db=%s, payment_id=%s",
                    payment_uuid,
                    transaction.payment_uuid,
                    payment_id,
                )
                return None
        else:
            async with self.session() as session:
                await Transaction.update(
                    session=session,
                    payment_id=payment_id,
                    payment_uuid=str(payment_uuid),
                )
            transaction.payment_uuid = str(payment_uuid)

        payment = await self._fetch_payment(payment_id)
        if not payment:
            return None

        api_uuid = payment.get("uuid")
        if api_uuid and str(api_uuid) != str(payment_uuid):
            logger.warning(
                "UrlPay callback uuid mismatch with API: payload=%s, api=%s, payment_id=%s",
                payment_uuid,
                api_uuid,
                payment_id,
            )

        if transaction.payment_uuid and api_uuid and str(transaction.payment_uuid) != str(api_uuid):
            logger.warning(
                "UrlPay callback uuid mismatch between database and API: db=%s, api=%s, payment_id=%s",
                transaction.payment_uuid,
                api_uuid,
                payment_id,
            )

        status_map = {
            "success": {3},
            "cancel": {4, 5, 6},
        }
        expected_status = status_map.get(payment_status)
        if expected_status and payment.get("status") not in expected_status:
            logger.warning(
                "UrlPay callback status mismatch: payload=%s, api_status=%s",
                payment_status,
                payment.get("status"),
            )
            return None

        return payment_id

    async def _fetch_payment(self, payment_id: Any) -> dict[str, Any] | None:
        if not self.config.urlpay.API_KEY:
            logger.error("UrlPay API key is missing.")
            return None

        headers = {
            "Authorization": f"Bearer {self.config.urlpay.API_KEY}",
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            url = f"{self._base_url}/v2/payments/{payment_id}"
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    logger.warning(
                        "Failed to fetch UrlPay payment: status=%s, payment_id=%s",
                        response.status,
                        payment_id,
                    )
                    return None

                result = await response.json()

        if not result.get("success"):
            logger.warning("UrlPay payment fetch returned unsuccessful response: %s", result)
            return None

        return result.get("payment")
