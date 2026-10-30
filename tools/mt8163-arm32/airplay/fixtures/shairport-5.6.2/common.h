APST_t string_to_service_type(const char *parameter, const char *setting_name);
void service_type_to_string(APST_t service_type, char *string_space);

void command_start(void);
void command_stop(void);
void command_execute(const char *command, const char *extra_argument, const int block);
void command_set_volume(double volume);
