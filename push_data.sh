#!/bin/bash
exec >> /home/Jules/push_data.log 2>&1
echo "--- $(date) ---"
cd /home/Jules/Automated_Watering_System
git checkout update_dashboard_data
cp /home/Jules/Automated_Watering_System/indoor/data_from_pico.txt data_from_pico.txt
git add data_from_pico.txt
git commit -m "data update" --allow-empty
git push origin update_dashboard_data
