def order_card(order):
    def telegram_contact(user_id, name, username):
        url = html.escape(
            contact_url(user_id, username),
            quote=True,
        )

        label = html.escape(name or "Telegram user")

        if username:
            label += " — @" + html.escape(username)

        return f'<a href="{url}">{label}</a>'

    creator = telegram_contact(
        order["creator_id"],
        order["creator_name"],
        order["creator_username"],
    )

    order_id = order["id"]

    # Заказ от клиента
    if order["creator_role"] == "client":
        details = html.escape(order["details"])

        price = (
            "€" + html.escape(order["price"])
            if order["price"]
            else "🤝 Συζητήσιμη"
        )

        text = (
            "🚕 <b>ΝΕΑ ΔΙΑΔΡΟΜΗ — ΠΕΛΑΤΗΣ</b>\n\n"
            f"{details}\n\n"
            f"💶 Προσφορά: <b>{price}</b>\n"
            f"👤 Πελάτης: {creator}\n"
            f"🔢 Αριθμός: #{order_id}"
        )

    # Голосовой заказ от водителя
    elif is_voice_order(order):
        text = (
            "🎤 <b>ΝΕΑ ΦΩΝΗΤΙΚΗ ΔΙΑΔΡΟΜΗ</b>\n\n"
            "🔊 Άκουσε το φωνητικό μήνυμα ακριβώς από πάνω.\n\n"
            f"👤 Από οδηγό: {creator}\n"
            f"🔢 Αριθμός: #{order_id}"
        )

        if order["details"] != "VOICE ORDER":
            text += "\n\n" + html.escape(order["details"])

    # Текстовый заказ от водителя
    else:
        details = html.escape(order["details"])

        text = (
            "🚕 <b>ΝΕΑ ΔΙΑΔΡΟΜΗ — ΟΔΗΓΟΣ</b>\n\n"
            f"{details}\n\n"
            f"👤 Από οδηγό: {creator}\n"
            f"🔢 Αριθμός: #{order_id}"
        )

    # Контакт водителя, который принял заказ
    if order["status"] == "taken":
        taker = telegram_contact(
            order["taker_id"],
            order["taker_name"],
            order["taker_username"],
        )

        text += (
            "\n\n✅ <b>Η ΔΙΑΔΡΟΜΗ ΔΟΘΗΚΕ</b>\n"
            f"🚖 Την πήρε: {taker}"
        )

    elif order["status"] == "cancelled":
        text += "\n\n❌ <b>Η ΔΙΑΔΡΟΜΗ ΑΚΥΡΩΘΗΚΕ</b>"

    return text
