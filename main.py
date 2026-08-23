import instaloader
from instaloader import NodeIterator
import csv
import requests
import time
import os


class InstagramFollowerChecker():
    def __init__(self, username):
        self.username = username
        
        self.profile = self.load_session(username)
        
    def load_session(self, username):
        loader = instaloader.Instaloader()
        loader.load_session_from_file(username, f"/root/.config/instaloader/session-{username}")

        profile = instaloader.Profile.own_profile(loader.context)
        
        return profile

    def fetch_current_followers(self):
        NodeIterator._graphql_page_length = 50
    
        followers = []
        
        for follower in self.profile.get_followers():
            followers.append(follower.username)
        
        return followers
    

    def update_csv(self, followers, csv_path="followers.csv",):
        with open(csv_path, "w", newline="", encoding="utf-8") as file: 
            csv.writer(file).writerows([[self.username] for self.username in followers])


    def read_csv(self, csv_path="followers.csv"):
        followers = []

        with open(csv_path, "r", newline="", encoding="utf-8") as file:
            rows = csv.reader(file)
            
            for row in rows:
                followers.append(row[0])
                
        return followers
    
    def send_push_notification(self, message, topic):
        print(message)
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": "Instagram unfollows", "Priority": "high"}
        )
    
    
        
if __name__ == "__main__":
    ntfy_topic = os.environ["TOPIC"]
    username = os.environ["USERNAME"]
    
    checker = InstagramFollowerChecker(username)

    while True:
        try:
            previous_followers = checker.read_csv()            
            current_followers = checker.fetch_current_followers()
            
            
            unfollowed_people = []
            
            for previous_follower in previous_followers:
                if previous_follower not in current_followers:
                    unfollowed_people.append(previous_follower)
                    
            
            if unfollowed_people:
                message = "Unfollowed you:\n" + "\n".join(unfollowed_people)
                checker.send_push_notification(message, ntfy_topic)
                checker.update_csv(current_followers)
            else:
                checker.send_push_notification("No unfollows", ntfy_topic)
            
            time.sleep(24*60*60)
        except Exception as e:
            checker.send_push_notification(e)