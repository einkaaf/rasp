#!/usr/bin/env python3
import time
import YB_Pcb_Car

print("Starting Raspbot...")

car = YB_Pcb_Car.YB_Pcb_Car()

try:
    print("Moving forward...")
    car.Car_Run(120, 120)
    time.sleep(3)

    print("Stopping...")
    car.Car_Stop()

except KeyboardInterrupt:
    print("Stopped by user")
    car.Car_Stop()

print("Done.")
