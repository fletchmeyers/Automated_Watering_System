#!/bin/bash
exec >> /home/Jules/push_data.log 2>&1
echo "--- $(date) ---"
cd /home/Jules/Automated_Watering_System
git checkout update_dashboard_data
cp /home/Jules/Documents/aws/data_from_pico.txt data_from_pico.txt
git add indoor/data_from_pico.txt
git commit -m "data update" --allow-empty
git push origin update_dashboard_data
