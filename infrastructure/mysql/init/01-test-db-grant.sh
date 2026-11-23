#!/bin/sh
# Dev only: runs once when the MySQL volume is first created.
# Lets the app user create/drop Django's test database (test_<name>) so pytest can run against MySQL.
set -e
mysql -uroot -p"$MYSQL_ROOT_PASSWORD" <<SQL
GRANT ALL PRIVILEGES ON \`test_${MYSQL_DATABASE}\`.* TO '${MYSQL_USER}'@'%';
FLUSH PRIVILEGES;
SQL
