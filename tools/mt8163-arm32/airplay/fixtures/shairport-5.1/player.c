    debug(4,
          "player_volume_without_notification: volume mode is %d, airplay volume is %.2f, "
          "software_attenuation dB: %.2f, hardware_attenuation dB: %.2f, muting "
          "is disabled.",
          volume_mode, airplay_volume, software_attenuation / 100.0, hardware_attenuation / 100.0);
  }
  // here, store the volume for possible use in the future
  config.airplay_volume = airplay_volume;
  conn->own_airplay_volume = airplay_volume;
  debug_mutex_unlock(&conn->volume_control_mutex, 4);
}

void player_volume(double airplay_volume, rtsp_conn_info *conn) {
  command_set_volume(airplay_volume);
  player_volume_without_notification(airplay_volume, conn);
}

void do_flush(uint32_t timestamp, rtsp_conn_info *conn) {

  debug(3, "do_flush: flush to %u.", timestamp);
  debug_mutex_lock(&conn->flush_mutex, 1000, 1);
  conn->flush_requested = 1;
  conn->flush_rtp_timestamp = timestamp; // flush all packets up to, but not including, this one.
  reset_input_flow_metrics(conn);
  debug_mutex_unlock(&conn->flush_mutex, 3);
}

void player_flush(uint32_t timestamp, rtsp_conn_info *conn) {
  debug(3, "player_flush");
  do_flush(timestamp, conn);
#ifdef CONFIG_CONVOLUTION
  convolver_clear_state();
#endif
#ifdef CONFIG_METADATA
  // only send a flush metadata message if the first packet has been seen -- it's a bogus message
  // otherwise
  if (conn->first_packet_timestamp) {
    char numbuf[32];
    snprintf(numbuf, sizeof(numbuf), "%u", timestamp);
    send_ssnc_metadata('pfls', numbuf, strlen(numbuf), 1); // contains cancellation points
  }
#endif
}

int player_play(rtsp_conn_info *conn) {
  debug(2, "Connection %d: player_play.", conn->connection_number);
  command_start(); // before startup, and before the prepare() method runs
  // give the output device as much advance warning as possible to get ready
  // and make sure it's done before launching the player thread
  if (config.output->prepare)
    config.output->prepare(); // give the backend its first chance to prepare itself, knowing it has
                              // access to the output device (i.e. knowing that it should not be in
                              // use by another program at this time).

  pthread_cleanup_debug_mutex_lock(&conn->player_create_delete_mutex, 5000, 1);
  if (conn->player_thread == NULL) {
    pthread_t *pt = malloc(sizeof(pthread_t));
    if (pt == NULL)
      die("Couldn't allocate space for pthread_t");

    int rc = named_pthread_create_with_priority(pt, 3, player_thread_func, (void *)conn,
                                                "player_%d", conn->connection_number);
    if (rc)
      debug(1, "Connection %d: error creating player_thread: %s", conn->connection_number,
            strerror(errno));
    conn->player_thread = pt; // set _after_ creation of thread
  } else {
    debug(1, "Connection %d: player thread already exists.", conn->connection_number);
  }
  pthread_cleanup_pop(1); // release the player_create_delete_mutex
#ifdef CONFIG_METADATA
  send_ssnc_metadata('pbeg', NULL, 0, 1); // contains cancellation points
#endif
  conn->is_playing = 1;
  return 0;
}

int player_stop(rtsp_conn_info *conn) {
  // note -- this may be called from another connection thread.
  debug(2, "Connection %d: player_stop.", conn->connection_number);
  int response = 0; // okay
  pthread_cleanup_debug_mutex_lock(&conn->player_create_delete_mutex, 5000, 4);
  pthread_t *pt = conn->player_thread;
  if (pt) {
    debug(3, "player_thread cancel...");
    conn->player_thread = NULL; // cleared _before_ cancelling of thread
    pthread_cancel(*pt);
    debug(3, "player_thread join...");
    if (pthread_join(*pt, NULL) == -1) {
      char errorstring[1024];
      strerror_r(errno, (char *)errorstring, sizeof(errorstring));
      debug(1, "Connection %d: error %d joining player thread: \"%s\".", conn->connection_number,
            errno, (char *)errorstring);
    } else {
      debug(2, "Connection %d: player_stop successful.", conn->connection_number);
    }
    free(pt);
    // reset_anchor_info(conn); // say the clock is no longer valid
#ifdef CONFIG_CONVOLUTION
    convolver_clear_state();
#endif
    response = 0; // deleted
  } else {
    debug(2, "Connection %d: no player thread.", conn->connection_number);
    response = -1; // already deleted or never created...
  }
  pthread_cleanup_pop(1); // release the player_create_delete_mutex
  if (response == 0) {    // if the thread was just stopped and deleted...
    conn->is_playing = 0;
/*
// this is done in the player cleanup handler
#ifdef CONFIG_AIRPLAY_2
    ptp_send_control_message_string("E"); // signify play is "E"nding
#endif
*/
#ifdef CONFIG_METADATA
    send_ssnc_metadata('pend', NULL, 0, 1); // contains cancellation points
#endif
    command_stop();
  }
  return response;
}
