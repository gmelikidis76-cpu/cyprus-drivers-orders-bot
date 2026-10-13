    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(start_alert_worker)
        .post_stop(stop_alert_worker)
        .post_shutdown(stop_alert_worker)
        .build()
    )
