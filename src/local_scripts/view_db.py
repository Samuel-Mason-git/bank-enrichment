from database_functions import init_db, print_stats, print_transactions, get_all_transactions

init_db(read_only=True)
print_stats()
print_transactions(get_all_transactions())
