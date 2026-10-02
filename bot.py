1089
1090
1091
1092
1093
1094
1095
1096
1097
1098
1099
1100
1101
1102
1103
1104
1105
1106
1107
1108
1109
1110
1111
1112
1113
1114
1115
1116
1117
1118
1119
1120
1121
1122
1123
1124
1125
1126
1127
1128
1129
1130
1131
1132
1133
1134
1135
1136
1137
1138
1139
1140
1141
1142
1143
1144
1145
1146
1147
1148
1149
1150
1151
1152
1153
1154
1155
1156
1157
1158
1159
1160
1161
1162
1163
1164
1165
1166
1167
1168
1169
1170
1171
1172
1173
1174
1175
1176
1177
1178
1179
1180
1181
1182
1183
1184
1185
1186
1187
1188
1189
1190
1191
1192
1193
1194
1195
1196
1197
1198
1199
1200
1201
1202
1203
1204
1205
1206
                text=private_client_text,
                reply_markup=client_driver_keyboard,
            )

        except Exception:
            pass

    # =====================================================
    # ВОДИТЕЛЬСКИЙ ЗАКАЗ
    # =====================================================

    else:

        try:
            private_text = (
                "✅ Η διαδρομή σου δόθηκε.\n\n"
                f"🚕 Οδηγός: {driver_name}\n"
                f"🆔 Telegram ID: {user.id}"
            )

            if user.username:
                private_text += (
                    f"\n💬 Telegram: "
                    f"@{user.username}"
                )

            await context.bot.send_message(
                chat_id=order["creator_id"],
                text=private_text,
            )

        except Exception:
            pass


# =========================================================
# ОШИБКИ
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Telegram error:",
        exc_info=context.error,
    )


# =========================================================
# ЗАПУСК
# =========================================================

def main():

    if not TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is not set"
        )

    app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "id",
            show_id,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            take_order,
            pattern=r"^take:\d+$",
        )
    )

    # Группа водителей
    app.add_handler(
        MessageHandler(
            filters.Chat(DRIVERS_GROUP_ID)
            & filters.TEXT
            & ~filters.COMMAND,
            driver_group_message,
        )
    )

    # Клиентская группа
    app.add_handler(
        MessageHandler(
            filters.Chat(CLIENTS_GROUP_ID)
            & filters.TEXT
            & ~filters.COMMAND,
            client_group_message,
        )
    )

    app.add_error_handler(
        error_handler
    )

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
