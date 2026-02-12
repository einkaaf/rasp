#!/usr/bin/env python3
# Move Raspbot forward for 3 seconds

import time
import YB_Pcb_Car


def main():
    print("Initializing car...")
    car = YB_Pcb_Car.YB_Pcb_Car()

    try:
        print("Moving forward...")
        car.Car_Run(120, 120)   # left speed, right speed (0–255)
        time.sleep(3)

        print("Stopping...")
        car.Car_Stop()

    except KeyboardInterrupt:
        print("Interrupted! Stopping car...")
        car.Car_Stop()

    finally:
        print("Done.")


if __name__ == "__main__":
    main()
